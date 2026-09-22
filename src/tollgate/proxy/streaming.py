"""Streaming passthrough: relay server-sent events without buffering the response.

The shape of the problem, and what each piece of this file does about it:

  * Events must reach the caller as they arrive. The relay yields each upstream chunk
    straight onward, and never accumulates one.
  * Token counts only appear inside the events, so the relay parses what passes through
    (see sse.py) while keeping only a small parse buffer.
  * Once the first event is sent the status code is spent: it's already a 200. A failure
    after that is delivered as an error event inside the stream instead.
  * The caller can hang up at any point. The upstream generated those tokens and will
    bill for them, so the row is still written, flagged, with the counts known so far.
  * A slow reader must not turn into unbounded memory. `yield` waits until the client
    has taken the chunk, and only then is the next one read from the upstream, so a slow
    reader slows the whole chain instead of filling a buffer.
  * The response has to be inspected on the way past, and a decision about whether it may be
    delivered has to be made before all of it has been seen. In `monitor` mode the relay
    scans and records; in `block` mode it runs a fixed number of bytes behind the upstream,
    so a credential found inside the unreleased window is dropped rather than delivered and
    the stream is cut off with an error event. The window is the length of leak that can
    still be contained, and it is paid for in time to first token: nothing is released until
    that much has arrived. See detect/service.py's ResponseInspection.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth import TenantContext
from tollgate.cache.keys import MISS
from tollgate.cache.replay import assemble, hit_headers, stream_events
from tollgate.cache.replay import response_media_type as replay_media_type
from tollgate.cache.service import DISABLED, CacheService
from tollgate.config import Settings
from tollgate.db.models import UsageRecord
from tollgate.detect.service import (
    DetectionService,
    apply_output_verdict,
    apply_verdict,
    prompt_blocked,
    response_blocked,
    response_text,
)
from tollgate.errors import GatewayError
from tollgate.limits import BudgetGuard
from tollgate.proxy.passthrough import (
    DROPPED_RESPONSE_HEADERS,
    RETRYABLE_STATUS,
    backoff_delay,
    build_upstream_request,
    map_transport_error,
)
from tollgate.proxy.sse import StreamScanner
from tollgate.telemetry import tracer
from tollgate.usage import (
    PriceBook,
    apply_usage_payload,
    run_even_if_cancelled,
    upstream_error_code,
    upstream_error_status,
    write_usage,
)

logger = logging.getLogger("tollgate.stream")

STREAM_ERROR_MESSAGE = "The upstream stream ended early. The response is incomplete."


def is_sse_request(request: Request) -> bool:
    """Gemini streams SSE when asked with ?alt=sse, and a JSON array otherwise."""
    return request.query_params.get("alt") == "sse"


def error_event(sse: bool, code: str) -> bytes:
    """An error delivered inside an already-started 200 response."""
    payload = json.dumps(
        {"error": {"source": "gateway", "code": code, "message": STREAM_ERROR_MESSAGE}}
    )
    # In array mode, close the array so the document is still parseable.
    return f"data: {payload}\r\n\r\n".encode() if sse else f",{payload}]".encode()


async def open_upstream_stream(
    client: httpx.AsyncClient, settings: Settings, request: httpx.Request, record: UsageRecord
) -> httpx.Response:
    """Start the upstream stream, retrying only while nothing has been sent to the caller."""
    for attempt in range(settings.upstream_max_retries + 1):
        is_last = attempt == settings.upstream_max_retries
        record.upstream_attempts = attempt + 1
        try:
            response = await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if is_last:
                raise
            delay = backoff_delay(settings, attempt, None)
        else:
            if response.status_code not in RETRYABLE_STATUS or is_last:
                return response
            delay = backoff_delay(settings, attempt, response.headers.get("retry-after"))
            if delay > settings.upstream_backoff_max_s:
                return response
            await response.aclose()
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def response_headers(upstream: httpx.Response, cache_status: str | None = None) -> dict[str, str]:
    headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() not in DROPPED_RESPONSE_HEADERS
    }
    if cache_status is not None:
        headers.update(hit_headers(cache_status))
    return headers


async def proxy_stream_generate_content(
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
        streamed=True,
        upstream_attempts=0,
        status_code=500,
        error_source="gateway",
        error_code="internal_error",
    )
    body = await request.body()
    # Before the upstream is opened. A streamed response's cost is unknown at this point,
    # so what is claimed here is an estimate built from the request's own ceiling; the
    # settlement at the end of the stream replaces it with the real figure.
    reservation = await budget.reserve(tenant, model, body)

    sse = is_sse_request(request)
    # Never raises; see detect/service.py, and the note there on why inspection runs before
    # the cache is asked. A blocked request is refused below, once there is something to
    # write the row with.
    verdict = await detection.inspect(mode=tenant.detection_mode, body=body)
    apply_verdict(record, verdict)

    lookup = (
        DISABLED
        if verdict.blocked
        else await cache.lookup(
            tenant_id=tenant.tenant_id,
            model=model,
            query_items=list(request.query_params.multi_items()),
            body=body,
        )
    )
    record.cache_status = lookup.status
    record.cache_similarity = lookup.similarity

    async def close_books() -> None:
        """One ledger row, then the settlement, for a request that opened no upstream stream.

        The two endings that use this - a refused request and one answered from the cache -
        have nothing to report about the provider, so `upstream_latency_ms` stays NULL: there
        was no call to time. `finish()` below is this plus the upstream span.
        """
        await write_usage(
            sessionmaker,
            pricebook,
            record,
            embedding_tokens=lookup.embedding_tokens,
            embedding_model=cache.embedding_model,
        )
        await budget.settle(reservation, record.cost_microcents)

    # --- refused before anything was sent: an ordinary error, and one row ----------------

    if verdict.blocked:
        error = prompt_blocked()
        record.status_code, record.error_source, record.error_code = (
            error.status_code,
            "gateway",
            error.code,
        )
        await close_books()
        raise error

    # --- a hit: no upstream, no span for one, and a stream built here --------------------

    if lookup.entry is not None:
        entry = lookup.entry
        # Whole in hand, so this is the unary decision rather than the streaming one: a
        # refusal happens before a single event, instead of cutting a stream off part way.
        # Raising here is still safe - nothing has been written to the wire yet.
        cached_output = detection.scan_response(mode=tenant.detection_mode, payload=entry.response)
        apply_output_verdict(record, cached_output)
        if cached_output.blocked:
            error = response_blocked()
            record.status_code, record.error_source, record.error_code = (
                error.status_code,
                "gateway",
                error.code,
            )
            await close_books()
            raise error

        record.status_code = 200
        record.error_source = record.error_code = None
        record.input_tokens = entry.input_tokens
        record.output_tokens = entry.output_tokens
        record.thoughts_tokens = entry.thoughts_tokens

        async def replay() -> AsyncIterator[bytes]:
            """The stored answer, re-emitted as events. See cache/replay.py.

            The bookkeeping is the same as the relay below and for the same reasons: a
            caller can hang up part way through a replay too, and the ledger row is owed
            either way - at zero cost, since nothing was generated.
            """
            try:
                for chunk in stream_events(entry, sse=sse):
                    yield chunk
            except (asyncio.CancelledError, GeneratorExit):
                record.client_disconnected = True
                raise
            finally:
                await run_even_if_cancelled(
                    close_books(), "ledger row and budget settlement for a cached stream"
                )

        return StreamingResponse(
            replay(),
            status_code=200,
            media_type=replay_media_type(sse),
            headers=hit_headers(lookup.status or ""),
        )

    # Started after the cache has been consulted, so the timings below cover the upstream
    # stream and not the lookup that preceded it. With the semantic tier on that lookup
    # is an embedding round trip, and folding it in here would report it as time to first
    # token - a number that is meant to say how long the caller waited on the model.
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    # Started by hand rather than with a `with`, because a stream's upstream time is
    # the whole relay and not the moment before the first byte. It is parented to the
    # root span here and closed in finish(), which every path below reaches exactly once.
    upstream_span = tracer.start_span("upstream", attributes={"tollgate.streamed": True})

    async def finish() -> None:
        """Close the books on this request: the upstream span, one ledger row, then the
        settlement.

        In that order, always. The settlement's script skips a month counter that has
        been evicted, on the understanding that the row is already in the ledger for the
        next reseed to find. If the write fails the settlement never runs, and the
        reservation is released by its lease instead.
        """
        upstream_span.set_attribute("tollgate.upstream.attempts", record.upstream_attempts)
        upstream_span.set_attribute("http.response.status_code", record.status_code)
        if record.upstream_ttfb_ms is not None:
            upstream_span.set_attribute("tollgate.upstream.ttfb_ms", record.upstream_ttfb_ms)
        upstream_span.end()
        await close_books()

    # --- before the first byte: ordinary error handling still applies --------------------

    upstream_request = build_upstream_request(
        client, request, settings, f"/v1beta/models/{model}:{method}", body
    )
    try:
        upstream = await open_upstream_stream(client, settings, upstream_request, record)
    except httpx.HTTPError as exc:
        error = map_transport_error(exc)
        record.status_code, record.error_source, record.error_code = (
            error.status_code,
            "gateway",
            error.code,
        )
        record.upstream_latency_ms = elapsed_ms()
        await finish()
        raise error from exc

    if not upstream.is_success:
        payload = await upstream.aread()
        await upstream.aclose()
        record.status_code = upstream.status_code
        record.error_source = "upstream"
        record.error_code = upstream_error_code(payload) or f"http_{upstream.status_code}"
        record.upstream_latency_ms = elapsed_ms()
        await finish()
        return Response(
            content=payload,
            status_code=upstream.status_code,
            headers=response_headers(upstream, lookup.status),
        )

    # --- from here the caller has a 200; problems travel inside the stream ---------------

    record.status_code = 200
    record.error_source = record.error_code = None
    # One inspection for this response, or None when this tenant is not being inspected. Made
    # here rather than inside the relay so that the mode is read once, at the start, and a
    # policy change mid-response cannot apply to half a stream.
    inspection = detection.response_stream(mode=tenant.detection_mode)

    async def relay() -> AsyncIterator[bytes]:
        scanner = StreamScanner(sse=sse)
        # The copy kept so this response can be cached, and the running count that bounds
        # it. Phase 3's promise is that memory stays flat under a long stream, and a bound
        # is exactly what keeps that promise: past `max_response_bytes` the collection is
        # dropped, the memory goes back, and the relay carries on having noticed nothing.
        # A response too large to cache is relayed and simply not stored.
        #
        # The bytes counted are the raw ones arriving from the upstream, which is more
        # than the parsed events retained - so the bound is conservative in the direction
        # that matters.
        collected: list[dict[str, Any]] | None = [] if lookup.status == MISS else None
        collected_bytes = 0
        # Bytes scanned but not yet released, and how many of them must stay unreleased.
        # Zero unless this tenant is in `block` mode: see ResponseInspection for why a
        # window that cannot stop anything is not worth the latency it costs.
        holdback = inspection.holdback_bytes if inspection is not None else 0
        pending = bytearray()
        try:
            async for chunk in upstream.aiter_raw():
                if record.upstream_ttfb_ms is None:
                    record.upstream_ttfb_ms = elapsed_ms()
                if collected is not None:
                    collected_bytes += len(chunk)
                    if collected_bytes > cache.max_response_bytes:
                        collected = None
                blocked = False
                for event in scanner.feed(chunk):
                    apply_usage_payload(record, event)  # running totals; the last one wins
                    if collected is not None:
                        collected.append(event)
                    if status := upstream_error_status(event):
                        # The upstream reported a failure inside the stream. Relay it as
                        # it is (the caller's SDK will raise on it) and record it: the
                        # response is incomplete, whatever the 200 says.
                        record.error_source, record.error_code = "upstream", status
                        collected = None  # never cache an answer that did not finish
                    if inspection is not None:
                        # The text of each event as it passes, not the raw bytes: the same
                        # string the unary path scans, so the two directions of one policy
                        # cannot disagree about what a response said. What is held back is
                        # measured in raw bytes, because raw bytes are what gets relayed.
                        blocked = inspection.feed(response_text(event)) or blocked

                if not holdback:
                    yield chunk  # waits for the caller to take it: backpressure, not buffering
                    continue

                pending += chunk
                if blocked:
                    # A finding inside the window that has not been relayed yet. The pending
                    # bytes are dropped rather than sent, the caller is told inside the stream
                    # that the rest was withheld, and the relay stops pulling from the
                    # upstream. Whatever was released before this point is already read.
                    record.error_source, record.error_code = "gateway", "response_blocked"
                    collected = None
                    pending.clear()
                    yield error_event(sse, "response_blocked")
                    break
                if len(pending) > holdback:
                    release = len(pending) - holdback
                    yield bytes(pending[:release])
                    del pending[:release]
            else:
                # The loop ran to its end rather than breaking on a finding, so the tail that
                # was being held is clean and goes now.
                if pending:
                    yield bytes(pending)

            if inspection is not None:
                apply_output_verdict(record, inspection.verdict())

            # Only here, after the loop has run to its end without raising. Every other
            # way out of this generator - a dropped upstream, a caller hanging up - leaves
            # a partial response, and a partial response is the one thing a cache must
            # never keep: it would be replayed as though it were whole.
            #
            # `output_findings` is checked too: an answer carrying a credential must not be
            # stored, or one leak is replayed to everybody who later asks the same question.
            if collected and record.error_code is None and not record.output_findings:
                await cache.store(
                    lookup,
                    tenant_id=tenant.tenant_id,
                    model=model,
                    response=assemble(collected),
                )
        except Exception as exc:
            # A 200 and some events are already out, so the status code can't say this.
            # Anything that goes wrong here (a dropped upstream connection, a bug of ours)
            # is reported inside the stream instead. Cancellation is a BaseException and
            # is handled separately below.
            record.error_source, record.error_code = "gateway", "upstream_stream_failed"
            logger.warning(
                "upstream stream failed", extra={"fields": {"error": type(exc).__name__}}
            )
            yield error_event(sse, "upstream_stream_failed")
        except (asyncio.CancelledError, GeneratorExit):
            # The caller hung up. Those tokens were still generated and still billed.
            record.client_disconnected = True
            record.error_source, record.error_code = "gateway", "client_disconnected"
            raise
        finally:
            record.upstream_latency_ms = elapsed_ms()
            await upstream.aclose()  # stop pulling from the provider immediately
            await run_even_if_cancelled(finish(), "ledger row and budget settlement")

    return StreamingResponse(
        relay(),
        status_code=200,
        headers=response_headers(upstream, lookup.status),
        media_type=upstream.headers.get("content-type"),
    )


def unsupported(method: str) -> GatewayError:
    return GatewayError(501, "unsupported_method", f"'{method}' is not supported yet.")
