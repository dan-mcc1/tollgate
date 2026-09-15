import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mock_upstream import main as mock
from tests.conftest import PROVIDER_KEY, Keys, usage_rows

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]


def auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


# --- authentication -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "no_key_sent"),
        (auth("tg_not_a_real_key"), "invalid_api_key"),
    ],
)
async def test_bad_keys_get_401_and_no_usage_row(
    gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    headers: dict[str, str],
    code: str,
) -> None:
    response = await gateway.post(URL, json=BODY, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == code
    assert await usage_rows(sessionmaker) == []
    assert mock.stats.calls["generateContent"] == 0


async def test_revoked_key_gets_401(gateway: httpx.AsyncClient, keys: Keys) -> None:
    response = await gateway.post(URL, json=BODY, headers=auth(keys.revoked))

    assert response.status_code == 401
    assert response.json()["error"] == {
        "source": "gateway",
        "code": "invalid_api_key",
        "message": "API key is unknown or revoked.",
    }


async def test_every_live_key_of_a_tenant_works(gateway: httpx.AsyncClient, keys: Keys) -> None:
    for key in (keys.live, keys.second_live):
        response = await gateway.post(URL, json=BODY, headers=auth(key))
        assert response.status_code == 200


# --- passthrough ----------------------------------------------------------------------


async def test_response_is_passed_through_and_usage_recorded(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    response = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidates"][0]["content"]["parts"][0]["text"].startswith("Mock response")

    [row] = await usage_rows(sessionmaker)
    usage = payload["usageMetadata"]
    assert (row.model, row.method, row.status_code) == (MODEL, "generateContent", 200)
    assert row.input_tokens == usage["promptTokenCount"]
    assert row.output_tokens == usage["candidatesTokenCount"]
    assert row.error_source is None
    assert row.upstream_attempts == 1
    assert row.upstream_latency_ms is not None


async def test_upstream_sees_provider_key_never_tenant_key(
    gateway: httpx.AsyncClient, keys: Keys
) -> None:
    await gateway.post(f"{URL}?key={keys.live}", json=BODY, headers=auth(keys.live))

    [received] = mock.stats.requests
    assert received["apiKey"] == mock.mask_key(PROVIDER_KEY)
    assert "key" not in received["query"]


async def test_provider_key_never_reaches_the_caller(
    gateway: httpx.AsyncClient, keys: Keys
) -> None:
    ok = await gateway.post(URL, json=BODY, headers=auth(keys.live))
    failed = await gateway.post(
        URL,
        json={"contents": [{"parts": [{"text": "[[mock:error=500]]"}]}]},
        headers=auth(keys.live),
    )

    for response in (ok, failed):
        assert PROVIDER_KEY not in response.text
        assert PROVIDER_KEY not in json.dumps(dict(response.headers))


async def test_upstream_error_passes_through_in_google_shape(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    body = {"contents": [{"parts": [{"text": "[[mock:error=400]]"}]}]}

    response = await gateway.post(URL, json=body, headers=auth(keys.live))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["status"] == "INVALID_ARGUMENT"
    assert "source" not in error  # an upstream error, not a gateway one
    [row] = await usage_rows(sessionmaker)
    assert (row.status_code, row.error_source, row.error_code) == (
        400,
        "upstream",
        "INVALID_ARGUMENT",
    )
    assert row.input_tokens is None
    assert row.upstream_attempts == 1  # 400 is not retryable


async def test_streaming_is_not_supported_yet(gateway: httpx.AsyncClient, keys: Keys) -> None:
    response = await gateway.post(
        f"/v1beta/models/{MODEL}:streamGenerateContent", json=BODY, headers=auth(keys.live)
    )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "unsupported_method"
