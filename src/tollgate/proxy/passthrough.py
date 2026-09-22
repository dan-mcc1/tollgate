"""Non-streaming passthrough: forward a Gemini request upstream, return the response as is.

The tenant's key is removed and the provider key is added here. It is the only place the
provider key is used, and nothing from the upstream response that could carry it is echoed.
"""

import asyncio
import json
import random
import time
from typing import Any

import httpx
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth import TenantContext
from tollgate.cache.exact import CachedResponse
from tollgate.cache.keys import request_payload
from tollgate.cache.replay import hit_headers
from tollgate.cache.service import DISABLED, CacheService
from tollgate.config import Settings
from tollgate.db.models import UsageRecord
from tollgate.detect.service import (
    DetectionService,
    apply_output_verdict,
    apply_verdict,
    prompt_blocked,
    response_blocked,
)
from tollgate.errors import GatewayError
from tollgate.limits import BudgetGuard
from tollgate.telemetry import stage
from tollgate.usage import PriceBook, apply_usage_metadata, upstream_error_code, write_usage

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Only these request headers go upstream. Everything else the caller sent (their key,
# cookies, Host, tracing headers) stays at the gateway.
FORWARDED_REQUEST_HEADERS = ("content-type", "accept", "user-agent", "x-goog-api-client")

# Response headers that describe the upstream connection rather than the body. httpx has
# already decompressed the body, and the ASGI server recomputes the length.
DROPPED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "date",
        "keep-alive",
        "server",
        "transfer-encoding",
    }
)


def create_upstream_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """The one shared upstream client. `transport` lets tests swap the network out."""
    return httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        transport=transport,
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            write=settings.upstream_write_timeout_s,
            read=settings.upstream_read_timeout_s,
            pool=settings.upstream_pool_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=settings.upstream_max_connections,
            max_keepalive_connections=settings.upstream_max_keepalive_connections,
        ),
    )


def backoff_delay(settings: Settings, attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retry number `attempt` (0-based)."""
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    ceiling = min(settings.upstream_backoff_max_s, settings.upstream_backoff_base_s * 2**attempt)
    return random.uniform(0, ceiling)


async def send_with_retries(
    client: httpx.AsyncClient, settings: Settings, request: httpx.Request, record: UsageRecord
) -> httpx.Response:
    """Send `request`, retrying 429s, 5xx responses and failed connections.

    Read and write timeouts are not retried: by then the upstream may already be
    generating (and billing for) the response, and a retry would pay for it twice.
    Counts attempts on `record` as it goes, so the count survives an exception.
    """
    for attempt in range(settings.upstream_max_retries + 1):
        is_last = attempt == settings.upstream_max_retries
        record.upstream_attempts = attempt + 1
        try:
            response = await client.send(request)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if is_last:
                raise
            delay = backoff_delay(settings, attempt, None)
        else:
            if response.status_code not in RETRYABLE_STATUS or is_last:
                return response
            delay = backoff_delay(settings, attempt, response.headers.get("retry-after"))
            if delay > settings.upstream_backoff_max_s:
                # The upstream wants a longer wait than we'll hold the caller for.
                return response
            await response.aclose()
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # the loop always returns or raises on its last pass


def build_upstream_request(
    client: httpx.AsyncClient, request: Request, settings: Settings, path: str, body: bytes
) -> httpx.Request:
    headers = {
        name: value
        for name in FORWARDED_REQUEST_HEADERS
        if (value := request.headers.get(name)) is not None
    }
    headers["x-goog-api-key"] = settings.gemini_api_key.get_secret_value()
    # A tenant may send their key as ?key=. It must not reach the upstream.
    params = httpx.QueryParams(
        [(k, v) for k, v in request.query_params.multi_items() if k != "key"]
    )
    return client.build_request("POST", path, content=body, headers=headers, params=params)


def serve_from_cache(record: UsageRecord, entry: CachedResponse, status: str) -> Response:
    """Answer from an entry the gateway already holds.

    The token counts are the ones the upstream reported when this answer was generated,
    so the ledger row says what the request would have consumed. Pricing turns that into
    `cost_avoided_microcents` and leaves `cost_microcents` at zero - the tenant is not
    charged twice for one generation. See PriceBook.apply.
    """
    record.status_code = 200
    record.error_source = record.error_code = None
    record.input_tokens = entry.input_tokens
    record.output_tokens = entry.output_tokens
    record.thoughts_tokens = entry.thoughts_tokens
    return Response(
        content=json.dumps(entry.response, ensure_ascii=False).encode(),
        status_code=200,
        media_type="application/json",
        headers=hit_headers(status),
    )


def screen_response(
    record: UsageRecord,
    detection: DetectionService,
    mode: str,
    payload: dict[str, Any] | None,
) -> None:
    """Scan a complete response before any of it is delivered, and refuse it if policy says so.

    Both endings of the unary path come through here - a fresh upstream response and a cached
    one - because the question is what is about to be delivered, not where it came from.

    Raising is safe at this point precisely because nothing has been written to the wire yet:
    the caller gets a 403 with a gateway error body instead of a 200 with a credential in it.
    The streamed path has no such luxury; see proxy/streaming.py.
    """
    verdict = detection.scan_response(mode=mode, payload=payload)
    apply_output_verdict(record, verdict)
    if verdict.blocked:
        raise response_blocked()


def to_response(upstream: httpx.Response, cache_status: str | None = None) -> Response:
    headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() not in DROPPED_RESPONSE_HEADERS
    }
    # On every request the cache saw, not only the ones it answered. A caller debugging
    # why its traffic is not being cached needs to see "bypass" as much as "hit", and a
    # benchmark needs to count outcomes without reading the ledger.
    if cache_status is not None:
        headers.update(hit_headers(cache_status))
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers)


async def proxy_generate_content(
    request: Request, tenant: TenantContext, model: str, method: str
) -> Response:
    settings: Settings = request.app.state.settings
    client: httpx.AsyncClient = request.app.state.http_client
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    pricebook: PriceBook = request.app.state.pricebook
    budget: BudgetGuard = request.app.state.budget
    cache: CacheService = request.app.state.cache
    detection: DetectionService = request.app.state.detection

    record = UsageRecord(
        tenant_id=tenant.tenant_id,
        api_key_id=tenant.api_key_id,
        model=model,
        method=method,
        upstream_attempts=0,
        status_code=500,
        error_source="gateway",
        error_code="internal_error",
    )
    body = await request.body()
    # Before anything reaches the upstream. A refusal here costs nothing and writes no
    # ledger row, because the request was never sent and so was never spent.
    reservation = await budget.reserve(tenant, model, body)

    # Then inspect what is being sent, which never raises: a blocked request is refused
    # below, inside the try, so that the refusal is recorded and the reservation released
    # like any other ending. See detect/service.py on why this runs before the cache.
    verdict = await detection.inspect(mode=tenant.detection_mode, body=body)
    apply_verdict(record, verdict)

    # None until the upstream call is actually about to happen. The finally below uses it
    # to decide whether there is an upstream latency to record at all: a blocked request
    # and a cache hit both reach the ledger without one, and a zero there would read as a
    # provider that answered instantly.
    started: float | None = None
    lookup = DISABLED

    try:
        if verdict.blocked:
            raise prompt_blocked()

        # The cache may make the upstream call unnecessary. See the note in
        # cache/service.py on why this comes after the reservation rather than before it.
        lookup = await cache.lookup(
            tenant_id=tenant.tenant_id,
            model=model,
            query_items=list(request.query_params.multi_items()),
            body=body,
        )
        record.cache_status = lookup.status
        record.cache_similarity = lookup.similarity

        # Started after the cache has been consulted, so `upstream_latency_ms` times the
        # upstream call and nothing else. Starting it earlier would fold the lookup - and,
        # with the semantic tier on, a whole embedding round trip - into a column named for
        # the provider, which is read as provider time by the dashboard and the README alike.
        started = time.perf_counter()

        if lookup.entry is not None:
            screen_response(record, detection, tenant.detection_mode, lookup.entry.response)
            return serve_from_cache(record, lookup.entry, lookup.status or "")
        upstream_request = build_upstream_request(
            client, request, settings, f"/v1beta/models/{model}:{method}", body
        )
        try:
            # One deadline over the whole exchange, retries included. Without it, a slow
            # upstream plus retries could outlast the load balancer's patience, and the
            # caller would get its generic 504 instead of an error that says what happened.
            #
            # This span is the one that earns its keep: subtract it from the root and what
            # remains is the gateway's own overhead, which is what the alert watches.
            with stage("upstream") as span:
                async with asyncio.timeout(settings.request_deadline_s):
                    response = await send_with_retries(client, settings, upstream_request, record)
                span.set_attribute("tollgate.upstream.attempts", record.upstream_attempts)
                span.set_attribute("http.response.status_code", response.status_code)
        except TimeoutError as exc:
            raise GatewayError(
                504, "gateway_deadline_exceeded", "The request took longer than the gateway allows."
            ) from exc
        except httpx.HTTPError as exc:
            raise map_transport_error(exc) from exc

        record.status_code = response.status_code
        if response.is_success:
            record.error_source = record.error_code = None
            apply_usage_metadata(record, response.content)
            payload = request_payload(response.content)
            # Before the cache is offered anything. A response carrying a credential must not
            # be stored, or the leak is replayed to every later caller asking the same
            # question - and this raises when policy says to withhold it, which skips the
            # store below without the call site needing a second condition.
            screen_response(record, detection, tenant.detection_mode, payload)
            # Offered to the cache before the response is handed back, so the next
            # identical request finds it. The ledger write below is already on this path,
            # so this is a second insert rather than the first; `store` refuses quietly
            # when the response is not one worth keeping, and never raises.
            if not record.output_findings:
                await cache.store(
                    lookup,
                    tenant_id=tenant.tenant_id,
                    model=model,
                    response=payload,
                )
        else:
            record.error_source = "upstream"
            record.error_code = (
                upstream_error_code(response.content) or f"http_{response.status_code}"
            )
        return to_response(response, lookup.status)

    except GatewayError as exc:
        record.status_code = exc.status_code
        record.error_source = "gateway"
        record.error_code = exc.code
        raise
    finally:
        if started is not None and lookup.entry is None:
            # Left NULL on a hit, and on a refusal: no upstream call was made, and zero
            # would read as one that returned instantly. telemetry.record_usage explains
            # what the overhead histogram does with that.
            record.upstream_latency_ms = round((time.perf_counter() - started) * 1000)
        # exactly one row, priced, whatever happened above
        await write_usage(
            sessionmaker,
            pricebook,
            record,
            embedding_tokens=lookup.embedding_tokens,
            embedding_model=cache.embedding_model,
        )
        await budget.settle(reservation, record.cost_microcents)


def map_transport_error(exc: httpx.HTTPError) -> GatewayError:
    """Turn an httpx failure into a gateway error. No upstream response exists here."""
    if isinstance(exc, httpx.PoolTimeout):
        # Every pooled connection was busy: our capacity problem, not the upstream's.
        return GatewayError(503, "gateway_overloaded", "The gateway is at connection capacity.")
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return GatewayError(502, "upstream_unreachable", "Could not connect to the upstream.")
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError(504, "upstream_timeout", "The upstream did not respond in time.")
    return GatewayError(502, "upstream_connection_failed", "The upstream connection failed.")
