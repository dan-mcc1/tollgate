"""Non-streaming passthrough: forward a Gemini request upstream, return the response as is.

The tenant's key is removed and the provider key is added here. It is the only place the
provider key is used, and nothing from the upstream response that could carry it is echoed.
"""

import asyncio
import random
import time

import httpx
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth import TenantContext
from tollgate.config import Settings
from tollgate.db.models import UsageRecord
from tollgate.errors import GatewayError
from tollgate.usage import apply_usage_metadata, upstream_error_code, write_usage

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


def to_response(upstream: httpx.Response) -> Response:
    headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() not in DROPPED_RESPONSE_HEADERS
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers)


async def proxy_generate_content(
    request: Request, tenant: TenantContext, model: str, method: str
) -> Response:
    settings: Settings = request.app.state.settings
    client: httpx.AsyncClient = request.app.state.http_client
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker

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
    started = time.perf_counter()

    try:
        upstream_request = build_upstream_request(
            client, request, settings, f"/v1beta/models/{model}:{method}", body
        )
        try:
            response = await send_with_retries(client, settings, upstream_request, record)
        except httpx.HTTPError as exc:
            raise map_transport_error(exc) from exc

        record.status_code = response.status_code
        if response.is_success:
            record.error_source = record.error_code = None
            apply_usage_metadata(record, response.content)
        else:
            record.error_source = "upstream"
            record.error_code = (
                upstream_error_code(response.content) or f"http_{response.status_code}"
            )
        return to_response(response)

    except GatewayError as exc:
        record.status_code = exc.status_code
        record.error_source = "gateway"
        record.error_code = exc.code
        raise
    finally:
        record.upstream_latency_ms = round((time.perf_counter() - started) * 1000)
        await write_usage(sessionmaker, record)  # exactly one row, whatever happened above


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
