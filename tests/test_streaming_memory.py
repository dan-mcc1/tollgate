"""Memory behaviour under a very long stream.

A module of its own: the upstream_transport fixture below replaces the mock upstream
for every test in its file.
"""

import json
import tracemalloc
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import Keys, usage_rows
from tollgate.db.models import CacheEntry

MODEL = "gemini-3.7-flash"
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]


def auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


# --- memory --------------------------------------------------------------------------------


class HugeStream:
    """An upstream that sends a very large response in many chunks."""

    def __init__(self, chunks: int, chunk_bytes: int) -> None:
        self.chunks, self.chunk_bytes = chunks, chunk_bytes

    def handle(self, request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            filler = "x" * self.chunk_bytes
            for i in range(self.chunks):
                event: dict[str, Any] = {"candidates": [{"content": {"parts": [{"text": filler}]}}]}
                if i == self.chunks - 1:
                    event["usageMetadata"] = {"candidatesTokenCount": 999}
                yield f"data: {json.dumps(event)}\r\n\r\n".encode()

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


@pytest.fixture
def upstream_transport() -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(HugeStream(chunks=400, chunk_bytes=64 * 1024).handle)


async def test_memory_stays_flat_over_a_very_long_response(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    received = 0
    tracemalloc.start()
    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=BODY, headers=auth(keys.live)
    ) as response:
        async for chunk in response.aiter_bytes():
            received += len(chunk)  # counted and dropped, never collected
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert received > 25_000_000, "the test should stream at least 25 MB"
    # Nothing accumulates: peak memory stays near one chunk, not near the response size.
    assert peak < 8_000_000, f"peak memory {peak:,} bytes suggests the response was buffered"
    [row] = await usage_rows(sessionmaker)
    assert row.output_tokens == 999  # usage still parsed out of the final event


class TestWithCachingOn:
    """The same guarantee, with the cache holding a copy of the response to store.

    Phase 6 introduced the one thing this file exists to forbid - a second reference to
    the response as it goes past - so the guarantee is re-measured with it switched on.
    What keeps it true is the bound: past `cache_max_response_bytes` the collection is
    dropped and the memory returned, so the peak is a property of the bound rather than
    of the response.
    """

    @pytest.fixture
    def cache_settings(self) -> dict[str, Any]:
        return {"cache_enabled": True, "cache_assumed_temperature": 0.0}

    async def test_memory_stays_flat_even_while_the_cache_is_collecting(
        self, live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
    ) -> None:
        received = 0
        tracemalloc.start()
        async with live_gateway.stream(
            "POST", f"{STREAM_URL}?alt=sse", json=BODY, headers=auth(keys.live)
        ) as response:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert received > 25_000_000
        assert peak < 8_000_000, f"peak memory {peak:,} bytes: the cache held on to the stream"
        # And nothing was stored, because the response outgrew the bound long ago.
        async with sessionmaker() as session:
            assert list((await session.scalars(select(CacheEntry))).all()) == []
