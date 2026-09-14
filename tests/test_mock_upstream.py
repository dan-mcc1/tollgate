import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from mock_upstream import main as mock

MODEL = mock.DEFAULT_MODEL
HEADERS = {"x-goog-api-key": "test-key"}
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent"
GENERATE_URL = f"/v1beta/models/{MODEL}:generateContent"


def prompt(text: str) -> dict[str, Any]:
    return {"contents": [{"role": "user", "parts": [{"text": text}]}]}


def sse_events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(mock, "LATENCY_MS", 0)
    monkeypatch.setattr(mock, "CHUNK_DELAY_MS", 0)
    mock.stats.reset()
    transport = httpx.ASGITransport(app=mock.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        yield c


async def test_missing_api_key_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(GENERATE_URL, json=prompt("hi"))

    assert response.status_code == 403
    assert response.json()["error"]["status"] == "PERMISSION_DENIED"


async def test_same_request_gives_same_text_but_new_response_id(
    client: httpx.AsyncClient,
) -> None:
    first = (await client.post(GENERATE_URL, json=prompt("hi"), headers=HEADERS)).json()
    second = (await client.post(GENERATE_URL, json=prompt("hi"), headers=HEADERS)).json()

    assert first["candidates"][0]["content"] == second["candidates"][0]["content"]
    assert first["responseId"] != second["responseId"]
    usage = first["usageMetadata"]
    assert usage["totalTokenCount"] == usage["promptTokenCount"] + usage["candidatesTokenCount"]


async def test_scripted_sse_sequence(client: httpx.AsyncClient) -> None:
    script = {
        "steps": [
            {"text": "Hel"},
            {"raw": ": keepalive\r\n\r\n"},
            {"text": "lo", "finishReason": "STOP"},
        ]
    }
    await client.post("/_mock/scripts", json=script)

    response = await client.post(f"{STREAM_URL}?alt=sse", json=prompt("x"), headers=HEADERS)

    assert response.headers["content-type"].startswith("text/event-stream")
    assert ": keepalive" in response.text
    events = sse_events(response.text)
    assert [e["candidates"][0]["content"]["parts"][0]["text"] for e in events] == ["Hel", "lo"]
    assert "finishReason" not in events[0]["candidates"][0]
    assert events[-1]["candidates"][0]["finishReason"] == "STOP"
    # Usage is cumulative: the last chunk carries the running total.
    assert events[-1]["usageMetadata"]["candidatesTokenCount"] == mock.estimate_tokens("Hello")


async def test_scripted_error_status(client: httpx.AsyncClient) -> None:
    await client.post("/_mock/scripts", json={"status": 429})

    response = await client.post(f"{STREAM_URL}?alt=sse", json=prompt("x"), headers=HEADERS)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"]["status"] == "RESOURCE_EXHAUSTED"


async def test_scripts_are_consumed_in_order(client: httpx.AsyncClient) -> None:
    await client.post("/_mock/scripts", json={"status": 503})
    await client.post("/_mock/scripts", json={"steps": [{"text": "ok", "finishReason": "STOP"}]})

    first = await client.post(GENERATE_URL, json=prompt("x"), headers=HEADERS)
    second = await client.post(GENERATE_URL, json=prompt("x"), headers=HEADERS)

    assert first.status_code == 503
    assert second.json()["candidates"][0]["content"]["parts"][0]["text"] == "ok"


async def test_scripted_midstream_disconnect(client: httpx.AsyncClient) -> None:
    await client.post("/_mock/scripts", json={"steps": [{"text": "partial"}, {"disconnect": True}]})

    with pytest.raises(mock.MidstreamDisconnect):
        await client.post(f"{STREAM_URL}?alt=sse", json=prompt("x"), headers=HEADERS)

    stats = (await client.get("/_mock/stats")).json()
    assert stats["outputTokens"] == mock.estimate_tokens("partial")


async def test_directive_fault_in_prompt(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"{STREAM_URL}?alt=sse", json=prompt("[[mock:error=503]] hi"), headers=HEADERS
    )

    assert response.status_code == 503
    assert response.json()["error"]["status"] == "UNAVAILABLE"


async def test_generated_stream_as_json_array(client: httpx.AsyncClient) -> None:
    response = await client.post(STREAM_URL, json=prompt("[[mock:tokens=60]] hi"), headers=HEADERS)

    chunks = json.loads(response.text)
    assert len(chunks) > 1
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"
