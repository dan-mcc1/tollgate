"""Streaming passthrough: relaying, accounting, failures and memory."""

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import Keys, make_settings, usage_rows
from tollgate.config import Settings

MODEL = "gemini-3.7-flash"
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]


def auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


def sse_payloads(chunks: list[bytes]) -> list[dict[str, Any]]:
    text = b"".join(chunks).decode()
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def settings(live_upstream: str) -> Settings:
    """Point the gateway at the mock running as a real server, so it really streams."""
    return make_settings().model_copy(update={"upstream_base_url": live_upstream})


@pytest.fixture
def upstream_transport() -> None:
    return None  # no in-process transport: reach the local mock server over loopback


async def queue_script(upstream: str, steps: list[dict[str, Any]]) -> None:
    async with httpx.AsyncClient(base_url=upstream) as client:
        await client.post("/_mock/scripts", json={"steps": steps})


# --- relaying -----------------------------------------------------------------------------


async def test_sse_stream_is_relayed_and_usage_recorded(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    live_upstream: str,
) -> None:
    await queue_script(
        live_upstream,
        [
            {"text": "Hel"},
            {
                "text": "lo",
                "finishReason": "STOP",
                "usage": {"promptTokenCount": 3, "candidatesTokenCount": 7},
            },
        ],
    )

    chunks: list[bytes] = []
    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=BODY, headers=auth(keys.live)
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    events = sse_payloads(chunks)
    assert [e["candidates"][0]["content"]["parts"][0]["text"] for e in events] == ["Hel", "lo"]

    [row] = await usage_rows(sessionmaker)
    assert row.streamed is True
    assert row.client_disconnected is False
    assert (row.status_code, row.input_tokens, row.output_tokens) == (200, 3, 7)
    assert row.error_source is None
    assert row.upstream_ttfb_ms is not None


async def test_events_arrive_before_the_stream_ends(
    live_gateway: httpx.AsyncClient, keys: Keys, live_upstream: str
) -> None:
    # The second event is 400 ms behind the first. If the gateway buffered the response,
    # the first chunk could not arrive before the whole thing is done.
    await queue_script(live_upstream, [{"text": "first"}, {"delayMs": 400, "text": "second"}])

    started = time.perf_counter()
    first_chunk_ms: float | None = None
    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=BODY, headers=auth(keys.live)
    ) as response:
        async for _ in response.aiter_bytes():
            if first_chunk_ms is None:
                first_chunk_ms = (time.perf_counter() - started) * 1000
    total_ms = (time.perf_counter() - started) * 1000

    assert first_chunk_ms is not None
    assert first_chunk_ms < 200, "first event was held back"
    assert total_ms > 400, "the whole stream should take at least as long as the delay"


async def test_json_array_stream_is_relayed_and_parsed(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    live_upstream: str,
) -> None:
    # Without ?alt=sse, Gemini streams a JSON array instead of events.
    await queue_script(
        live_upstream,
        [
            {"text": "a"},
            {"text": "b", "finishReason": "STOP", "usage": {"candidatesTokenCount": 4}},
        ],
    )

    async with live_gateway.stream(
        "POST", STREAM_URL, json=BODY, headers=auth(keys.live)
    ) as response:
        body = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert json.loads(body)[-1]["candidates"][0]["finishReason"] == "STOP"
    [row] = await usage_rows(sessionmaker)
    assert (row.streamed, row.output_tokens) == (True, 4)


# --- failures ------------------------------------------------------------------------------


async def test_upstream_failure_midstream_reaches_client_and_ledger(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    live_upstream: str,
) -> None:
    await queue_script(
        live_upstream,
        [{"text": "partial", "usage": {"candidatesTokenCount": 2}}, {"disconnect": True}],
    )

    chunks: list[bytes] = []
    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=BODY, headers=auth(keys.live)
    ) as response:
        assert response.status_code == 200  # already sent; the error can only go inside
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    events = sse_payloads(chunks)
    assert events[0]["candidates"][0]["content"]["parts"][0]["text"] == "partial"
    assert events[-1]["error"] == {
        "source": "gateway",
        "code": "upstream_stream_failed",
        "message": "The upstream stream ended early. The response is incomplete.",
    }

    [row] = await usage_rows(sessionmaker)
    assert (row.status_code, row.error_source, row.error_code) == (
        200,
        "gateway",
        "upstream_stream_failed",
    )
    assert row.output_tokens == 2  # what the upstream reported before it died


async def test_client_hangup_still_writes_an_abandoned_row(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    live_upstream: str,
) -> None:
    await queue_script(
        live_upstream,
        [
            {"text": "one", "usage": {"candidatesTokenCount": 5}},
            {"delayMs": 5000, "text": "never read"},
        ],
    )

    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=BODY, headers=auth(keys.live)
    ) as response:
        async for _ in response.aiter_bytes():
            break  # the caller walks away mid-stream

    for _ in range(50):  # the row is written by a shielded background task
        rows = await usage_rows(sessionmaker)
        if rows:
            break
        await asyncio.sleep(0.05)

    [row] = rows
    assert row.client_disconnected is True
    assert (row.streamed, row.status_code, row.error_code) == (True, 200, "client_disconnected")
    assert row.output_tokens == 5  # generated, and billed, before the caller left


async def test_upstream_error_before_the_stream_starts_is_a_normal_error(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    live_upstream: str,
) -> None:
    body = {"contents": [{"parts": [{"text": "[[mock:error=400]]"}]}]}

    response = await live_gateway.post(f"{STREAM_URL}?alt=sse", json=body, headers=auth(keys.live))

    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    [row] = await usage_rows(sessionmaker)
    assert (row.status_code, row.error_source, row.streamed) == (400, "upstream", True)


async def test_upstream_error_event_inside_a_stream_is_recorded(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    live_upstream: str,
) -> None:
    # Seen in production: the connection stays healthy and Gemini sends an error object as
    # an ordinary event. Nothing breaks, so only reading the events catches it.
    google_503 = json.dumps(
        {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}}
    )
    await queue_script(
        live_upstream,
        [
            {"text": "partial answer", "usage": {"candidatesTokenCount": 3}},
            {"raw": f"data: {google_503}\r\n\r\n"},
        ],
    )

    chunks: list[bytes] = []
    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=BODY, headers=auth(keys.live)
    ) as response:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    # Relayed untouched, in the provider's own shape.
    assert sse_payloads(chunks)[-1]["error"]["status"] == "UNAVAILABLE"

    [row] = await usage_rows(sessionmaker)
    assert (row.status_code, row.error_source, row.error_code) == (200, "upstream", "UNAVAILABLE")
    assert row.output_tokens == 3  # billed for what arrived before the failure
