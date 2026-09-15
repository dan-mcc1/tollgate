import httpx

from tollgate.auth import KEY_PREFIX, generate_key, hash_key
from tollgate.main import app


def test_hash_is_stable_sha256() -> None:
    assert hash_key("tg_abc") == hash_key("tg_abc")
    assert len(hash_key("tg_abc")) == 64


def test_generated_key_matches_its_hash_and_prefix() -> None:
    key = generate_key()

    assert key.plaintext.startswith(KEY_PREFIX)
    assert key.hash == hash_key(key.plaintext)
    assert key.plaintext.startswith(key.prefix)
    assert generate_key().plaintext != key.plaintext


async def test_missing_key_returns_gateway_401() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tollgate") as client:
        response = await client.post(
            "/v1beta/models/gemini-3.7-flash:generateContent", json={"contents": []}
        )

    assert response.status_code == 401
    assert response.json()["error"] == {
        "source": "gateway",
        "code": "missing_api_key",
        "message": "Send a Tollgate key in x-goog-api-key.",
    }
