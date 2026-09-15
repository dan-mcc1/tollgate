import json
import logging

import httpx
import pytest

from tests.conftest import Keys
from tollgate.logs import JsonFormatter, request_id_var, tenant_var

URL = "/v1beta/models/gemini-3.7-flash:generateContent"
BODY = {"contents": [{"parts": [{"text": "a secret prompt that must never be logged"}]}]}


def access_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [r.fields for r in caplog.records if r.name == "tollgate.access"]  # type: ignore[attr-defined]


async def test_response_carries_a_generated_request_id(gateway: httpx.AsyncClient) -> None:
    response = await gateway.get("/livez")

    assert len(response.headers["x-request-id"]) == 32


async def test_well_formed_incoming_request_id_is_kept(gateway: httpx.AsyncClient) -> None:
    response = await gateway.get("/livez", headers={"x-request-id": "client-abc.123"})

    assert response.headers["x-request-id"] == "client-abc.123"


async def test_malformed_incoming_request_id_is_replaced(gateway: httpx.AsyncClient) -> None:
    response = await gateway.get("/livez", headers={"x-request-id": "bad id\nwith newline"})

    assert response.headers["x-request-id"] != "bad id\nwith newline"
    assert len(response.headers["x-request-id"]) == 32


async def test_access_log_has_request_id_and_tenant_but_no_secrets(
    gateway: httpx.AsyncClient, keys: Keys, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tollgate.access")

    response = await gateway.post(
        f"{URL}?key={keys.live}", json=BODY, headers={"x-goog-api-key": keys.live}
    )

    [entry] = access_records(caplog)
    assert entry["request_id"] == response.headers["x-request-id"]
    assert entry["tenant"] == "acme"
    assert entry["status"] == 200
    assert entry["path"] == URL
    logged = json.dumps(entry)
    assert keys.live not in logged
    assert "secret prompt" not in logged


async def test_health_checks_are_not_access_logged(
    gateway: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tollgate.access")

    await gateway.get("/livez")
    await gateway.get("/readyz")

    assert access_records(caplog) == []


def test_formatter_writes_one_json_object_with_request_context() -> None:
    record = logging.LogRecord(
        "tollgate.test", logging.WARNING, __file__, 1, "hello %s", ("you",), None
    )
    record.fields = {"attempts": 3}
    request_token, tenant_token = request_id_var.set("req-1"), tenant_var.set("acme")
    try:
        line = JsonFormatter().format(record)
    finally:
        request_id_var.reset(request_token)
        tenant_var.reset(tenant_token)

    entry = json.loads(line)
    assert "\n" not in line
    assert entry["message"] == "hello you"
    assert entry["level"] == "warning"
    assert (entry["request_id"], entry["tenant"], entry["attempts"]) == ("req-1", "acme", 3)
