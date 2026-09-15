"""Retries, timeouts and connection failures, against a scripted fake upstream."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import Keys, usage_rows

URL = "/v1beta/models/gemini-3.7-flash:generateContent"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Step = httpx.Response | Exception


class ScriptedUpstream:
    """Returns (or raises) each queued step in order and counts the calls it received."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def upstream() -> ScriptedUpstream:
    return ScriptedUpstream()


@pytest.fixture
def upstream_transport(upstream: ScriptedUpstream) -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(upstream.handle)


def ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": "hi"}], "role": "model"}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5},
        },
    )


def google_error(code: int, status: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        code, json={"error": {"code": code, "status": status}}, headers=headers or {}
    )


async def post(gateway: httpx.AsyncClient, keys: Keys) -> httpx.Response:
    return await gateway.post(URL, json=BODY, headers={"x-goog-api-key": keys.live})


async def test_retries_503_then_succeeds_with_one_usage_row(
    gateway: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    keys: Keys,
    upstream: ScriptedUpstream,
) -> None:
    upstream.steps = [google_error(503, "UNAVAILABLE"), google_error(503, "UNAVAILABLE"), ok()]

    response = await post(gateway, keys)

    assert response.status_code == 200
    assert upstream.calls == 3
    [row] = await usage_rows(sessionmaker)
    assert (row.status_code, row.upstream_attempts, row.output_tokens) == (200, 3, 5)


async def test_gives_up_after_max_retries_and_returns_upstream_error(
    gateway: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    keys: Keys,
    upstream: ScriptedUpstream,
) -> None:
    upstream.steps = [google_error(429, "RESOURCE_EXHAUSTED") for _ in range(3)]

    response = await post(gateway, keys)

    assert response.status_code == 429
    assert response.json()["error"]["status"] == "RESOURCE_EXHAUSTED"
    [row] = await usage_rows(sessionmaker)
    assert (row.upstream_attempts, row.error_source) == (3, "upstream")


async def test_long_retry_after_is_not_waited_out(
    gateway: httpx.AsyncClient, keys: Keys, upstream: ScriptedUpstream
) -> None:
    upstream.steps = [google_error(429, "RESOURCE_EXHAUSTED", {"retry-after": "60"})]

    response = await post(gateway, keys)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"  # passed on so the caller can back off
    assert upstream.calls == 1


async def test_read_timeout_is_a_clean_gateway_504_and_not_retried(
    gateway: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    keys: Keys,
    upstream: ScriptedUpstream,
) -> None:
    upstream.steps = [httpx.ReadTimeout("timed out")]

    response = await post(gateway, keys)

    assert response.status_code == 504
    assert response.json() == {
        "error": {
            "source": "gateway",
            "code": "upstream_timeout",
            "message": "The upstream did not respond in time.",
        }
    }
    assert upstream.calls == 1
    [row] = await usage_rows(sessionmaker)
    assert (row.status_code, row.error_source, row.error_code) == (
        504,
        "gateway",
        "upstream_timeout",
    )
    assert row.input_tokens is None


async def test_connection_failures_are_retried_then_reported_as_502(
    gateway: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    keys: Keys,
    upstream: ScriptedUpstream,
) -> None:
    upstream.steps = [httpx.ConnectError("refused") for _ in range(3)]

    response = await post(gateway, keys)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unreachable"
    [row] = await usage_rows(sessionmaker)
    assert row.upstream_attempts == 3


async def test_pool_exhaustion_is_reported_as_gateway_overload(
    gateway: httpx.AsyncClient, keys: Keys, upstream: ScriptedUpstream
) -> None:
    upstream.steps = [httpx.PoolTimeout("no free connection")]

    response = await post(gateway, keys)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gateway_overloaded"
