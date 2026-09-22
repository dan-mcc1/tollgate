"""Output scanning: the shapes, the boundary problem, and what the two modes really do.

Three groups. The rules on their own, including the benign strings they must not fire on -
a scanner's false positives land on responses a customer is already reading, which is the
most expensive place for a security control to be wrong. Then the incremental scanner, which
is where a credential split across two streamed events is either caught or lost. Then the
gateway, where the interesting claim lives: that `block` mode contains a leak rather than
merely noticing one, and that `monitor` mode does not pretend to.
"""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mock_upstream import main as mock
from tests.conftest import Keys, usage_rows
from tests.test_detection import set_mode
from tests.test_limits import auth
from tests.test_telemetry import collect, telemetry_text
from tollgate.detect import scanner
from tollgate.detect.service import (
    ALLOWED,
    BLOCKED,
    CLEAN,
    FLAGGED,
    MODE_BLOCK,
    MODE_OFF,
    TRUNCATED,
    DetectionService,
    ResponseInspection,
    response_text,
)

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
Sessionmaker = async_sessionmaker[AsyncSession]

# AWS's own documented example key, which is what the mock returns for [[mock:leak]].
LEAKED_KEY = "AKIAIOSFODNN7EXAMPLE"
LEAK = "[[mock:leak]]"


def body(text: str) -> dict[str, Any]:
    return {"contents": [{"role": "user", "parts": [{"text": text}]}]}


@pytest.fixture
def detection_settings() -> dict[str, Any]:
    return {"detection_enabled": True}


# --- the rules -----------------------------------------------------------------------------

LEAKS = [
    ("aws_access_key_id", f"deploy with {LEAKED_KEY} and the secret below"),
    (
        "aws_secret_access_key",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ),
    ("google_api_key", "key AIzaSyB00000000000000000000000000000000 works"),
    ("tollgate_api_key", "your key is tg_" + "a" * 43),
    ("github_token", "ghp_" + "b" * 36),
    ("openai_api_key", "sk-proj-abcdefghijklmnopqrstuvwxyz012345"),
    ("slack_token", "xoxb-123456789012-abcdefghijkl"),
    ("stripe_key", "sk_live_abcdefghijklmnopqrst"),
    ("bearer_token", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz.012345"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"),
    ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA"),
    ("email", "write to jane.doe@example.com about it"),
    ("phone", "call (555) 010-0199 between nine and five"),
    ("us_ssn", "SSN: 123-45-6789"),
    ("credit_card", "card: 4111 1111 1111 1111"),
]

# Responses a real application produces. Each one is close to a rule without being a finding,
# because these are the false positives that get output scanning switched off.
BENIGN = [
    "A reverse proxy sits in front of your application and forwards requests to it.",
    "The build produced 16 artifacts in 1234567890 ms.",
    "Commit a94a8fe5ccb19ba61c4c0873d391e987982fbbd3 is the one you want.",
    "Set AWS_REGION=us-east-1 and AWS_PROFILE=staging in your environment.",
    "Order 4111111111111 shipped on 2026-01-02 at 14:33:02 UTC.",
    "The part number is 123-00-4567 and the revision is 900-12-3456.",
    "Use Authorization: Bearer <token> as the header, with your own token.",
    "Base64 of 'hello world' is aGVsbG8gd29ybGQ=.",
]


@pytest.mark.parametrize(("rule", "text"), LEAKS, ids=[rule for rule, _ in LEAKS])
def test_every_rule_finds_the_shape_it_is_for(rule: str, text: str) -> None:
    assert rule in scanner.findings_in(text)


@pytest.mark.parametrize("text", BENIGN)
def test_ordinary_responses_produce_no_findings(text: str) -> None:
    """The hard half of the rule set. A commit sha is forty hex characters, a part number
    looks like an SSN, an order id is thirteen digits, and none of them are a leak."""
    assert scanner.findings_in(text) == []


def test_a_card_number_needs_a_valid_check_digit() -> None:
    """Luhn is what separates "sixteen digits" from "a card number". Without it this rule
    fires on every long identifier there is."""
    assert scanner.findings_in("card 4111 1111 1111 1111") == ["credit_card"]
    assert scanner.findings_in("card 4111 1111 1111 1112") == []


def test_an_impossible_social_security_number_is_not_one() -> None:
    assert scanner.findings_in("123-45-6789") == ["us_ssn"]
    for impossible in ("000-45-6789", "666-45-6789", "912-45-6789", "123-00-6789", "123-45-0000"):
        assert scanner.findings_in(impossible) == [], impossible


def test_forty_base64_characters_alone_are_not_a_secret() -> None:
    """The reason aws_secret_access_key insists on the name beside it: that shape is also a
    hash, an id, and a sentence in base64."""
    assert scanner.findings_in("digest wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY") == []
    assert scanner.findings_in("aws secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY") == [
        "aws_secret_access_key"
    ]


def test_a_finding_is_a_rule_id_and_never_the_text() -> None:
    """A finding travels to the ledger, a span and a metric label. If it carried the match,
    every one of those would be holding a live credential."""
    findings = scanner.findings_in(f"the key is {LEAKED_KEY}")

    assert findings == ["aws_access_key_id"]
    assert LEAKED_KEY not in "".join(findings)


def test_findings_are_sorted_and_unique() -> None:
    """The ledger stores them as one string, so the same response has to produce the same
    string - otherwise two identical leaks look like two different ones."""
    text = f"{LEAKED_KEY} and jane@example.com and {LEAKED_KEY} and bob@example.com"

    assert scanner.findings_in(text) == ["aws_access_key_id", "email"]


# --- the boundary problem --------------------------------------------------------------------


def test_a_secret_split_across_two_pieces_is_still_found() -> None:
    """The whole reason the incremental scanner keeps an overlap. A model streams
    `AKIAIOSF` and then `ODNN7EXAMPLE`, and neither half matches anything."""
    incremental = scanner.Incremental()

    assert incremental.feed("the key is AKIAIOSF") == []
    assert incremental.feed("ODNN7EXAMPLE, keep it safe") == ["aws_access_key_id"]


def test_a_finding_is_reported_once_however_often_it_is_rescanned() -> None:
    """The overlap means text is scanned more than once. A caller reacting to `feed` must
    not be told about the same finding twice."""
    incremental = scanner.Incremental()

    first = incremental.feed(f"key {LEAKED_KEY}")
    second = incremental.feed(" and nothing else")

    assert first == ["aws_access_key_id"]
    assert second == []
    assert incremental.findings == ["aws_access_key_id"]


def test_the_overlap_is_longer_than_the_longest_pattern() -> None:
    """A pattern longer than the overlap could straddle it and be found in neither scan."""
    longest = max(len(item.pattern.pattern) for item in scanner.RULES)

    assert longest < scanner.OVERLAP_CHARS


def test_text_is_taken_from_the_parts_a_response_is_delivering() -> None:
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": "first "}, {"text": "second"}], "role": "model"}}
        ]
    }

    assert response_text(payload) == "first second"
    assert response_text({}) == ""


# --- the two modes, in process ----------------------------------------------------------------


def leaking_response() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": f"here you go: {LEAKED_KEY}"}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 9},
    }


def test_monitor_mode_records_a_finding_and_delivers_anyway() -> None:
    detection = DetectionService(enabled=True)

    verdict = detection.scan_response(mode="monitor", payload=leaking_response())

    assert verdict.verdict == FLAGGED
    assert verdict.findings == ("aws_access_key_id",)
    assert verdict.action == ALLOWED
    assert not verdict.blocked


def test_block_mode_refuses_the_response() -> None:
    detection = DetectionService(enabled=True)

    verdict = detection.scan_response(mode=MODE_BLOCK, payload=leaking_response())

    assert verdict.blocked
    assert verdict.action == BLOCKED


def test_nothing_is_recorded_when_nobody_scanned() -> None:
    detection = DetectionService(enabled=True)
    disabled = DetectionService(enabled=False)

    for verdict in (
        detection.scan_response(mode=MODE_OFF, payload=leaking_response()),
        disabled.scan_response(mode=MODE_BLOCK, payload=leaking_response()),
    ):
        assert verdict.verdict is None
        assert verdict.action is None
        assert not verdict.blocked


def test_a_clean_response_is_recorded_as_scanned() -> None:
    detection = DetectionService(enabled=True)
    payload = {"candidates": [{"content": {"parts": [{"text": "a reverse proxy forwards"}]}}]}

    verdict = detection.scan_response(mode=MODE_BLOCK, payload=payload)

    assert verdict.verdict == CLEAN
    assert verdict.findings == ()


def test_the_findings_column_is_one_sorted_string() -> None:
    detection = DetectionService(enabled=True)
    payload = {
        "candidates": [{"content": {"parts": [{"text": f"{LEAKED_KEY} and jane.doe@example.com"}]}}]
    }

    verdict = detection.scan_response(mode="monitor", payload=payload)

    assert verdict.joined() == "aws_access_key_id,email"


# --- the hold-back window ---------------------------------------------------------------------


def test_monitor_mode_holds_nothing_back() -> None:
    """Delaying a stream in order to do nothing about what is found would buy latency and no
    containment. This is what "monitor does not pretend to enforce" means in code."""
    detection = DetectionService(enabled=True, holdback_bytes=1024)

    watching = detection.response_stream(mode="monitor")
    blocking = detection.response_stream(mode=MODE_BLOCK)

    assert watching is not None and watching.holdback_bytes == 0
    assert blocking is not None and blocking.holdback_bytes == 1024


def test_a_watching_inspection_never_asks_the_relay_to_stop() -> None:
    inspection = ResponseInspection(blocking=False, holdback_bytes=1024)

    assert inspection.feed(f"key {LEAKED_KEY}") is False
    verdict = inspection.verdict()
    assert verdict.verdict == FLAGGED
    assert verdict.action == ALLOWED  # noticed, not contained


def test_a_blocking_inspection_stops_at_the_first_finding() -> None:
    inspection = ResponseInspection(blocking=True, holdback_bytes=1024)

    assert inspection.feed("all is well so far") is False
    assert inspection.feed(f"and here is {LEAKED_KEY}") is True
    assert inspection.feed("more text") is True  # it stays stopped
    assert inspection.verdict().action == TRUNCATED


def test_a_scanner_that_fails_does_not_take_the_response_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails open, like the input direction. The response is not the thing at fault."""

    def explode(text: str) -> list[str]:
        raise RuntimeError("pattern catastrophe")

    monkeypatch.setattr(scanner, "findings_in", explode)
    inspection = ResponseInspection(blocking=True, holdback_bytes=1024)

    assert inspection.feed("anything") is False
    assert inspection.verdict().verdict == "error"


# --- through the gateway ----------------------------------------------------------------------


async def test_a_leaking_response_is_recorded_and_still_delivered_in_monitor_mode(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    response = await gateway.post(URL, json=body(LEAK), headers=auth(keys.live))

    assert response.status_code == 200
    assert LEAKED_KEY in response.text  # monitor mode does not intervene
    [row] = await usage_rows(sessionmaker)
    assert row.output_verdict == FLAGGED
    assert row.output_action == ALLOWED
    assert "aws_access_key_id" in (row.output_findings or "")


async def test_a_leaking_response_is_withheld_in_block_mode(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The unary path can refuse properly: nothing has been written to the wire, so the
    caller gets an error instead of a credential."""
    await set_mode(sessionmaker, MODE_BLOCK)

    response = await gateway.post(URL, json=body(LEAK), headers=auth(keys.live))

    assert response.status_code == 403
    assert LEAKED_KEY not in response.text
    assert response.json()["error"]["code"] == "response_blocked"
    [row] = await usage_rows(sessionmaker)
    assert (row.output_verdict, row.output_action) == (FLAGGED, BLOCKED)
    # The provider generated those tokens and billed for them, whatever the gateway did next.
    assert row.output_tokens and row.cost_microcents


async def test_a_withheld_response_names_nothing_it_found(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await set_mode(sessionmaker, MODE_BLOCK)

    response = await gateway.post(URL, json=body(LEAK), headers=auth(keys.live))

    lowered = response.text.lower()
    for rule in (item.id for item in scanner.RULES):
        assert rule not in lowered


@pytest.mark.parametrize("cache_settings", [{"cache_enabled": True, "cache_max_temperature": 1.0}])
async def test_a_response_with_findings_is_never_cached(
    gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    cache_settings: dict[str, Any],
) -> None:
    """Caching a leak replays it to everybody who later asks the same question, which turns
    one bad response into a permanent one."""
    first = await gateway.post(URL, json=body(LEAK), headers=auth(keys.live))
    second = await gateway.post(URL, json=body(LEAK), headers=auth(keys.live))

    assert first.status_code == second.status_code == 200
    rows = await usage_rows(sessionmaker)
    assert [row.cache_status for row in rows] == ["miss", "miss"]
    assert mock.stats.calls["generateContent"] == 2


async def test_output_findings_are_counted_for_the_dashboard(
    gateway: httpx.AsyncClient, keys: Keys, meter: Any
) -> None:
    await gateway.post(URL, json=body(LEAK), headers=auth(keys.live))

    recorded = collect(meter)
    [scan] = recorded["tollgate.output.scans"]
    assert scan.attributes is not None and scan.attributes["verdict"] == FLAGGED
    found = {point.attributes["finding"] for point in recorded["tollgate.output.findings"]}
    assert "aws_access_key_id" in found


# --- through the gateway, streamed ------------------------------------------------------------


async def read_stream(client: httpx.AsyncClient, keys: Keys, prompt: str) -> tuple[int, str]:
    async with client.stream(
        "POST", STREAM_URL, json=body(prompt), headers=auth(keys.live)
    ) as response:
        received = b"".join([chunk async for chunk in response.aiter_bytes()])
    return response.status_code, received.decode()


async def test_a_monitored_stream_is_relayed_whole_and_the_finding_recorded(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    status, received = await read_stream(live_gateway, keys, LEAK)

    assert status == 200
    assert LEAKED_KEY in received
    [row] = await usage_rows(sessionmaker)
    assert (row.output_verdict, row.output_action) == (FLAGGED, ALLOWED)


async def test_a_blocked_stream_is_cut_off_before_the_secret_is_relayed(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The claim the hold-back window exists to make good on. The response has already
    started with a 200, so the refusal arrives as an error event inside the stream - and the
    credential itself never leaves, because it was still being held when it was found."""
    await set_mode(sessionmaker, MODE_BLOCK)

    status, received = await read_stream(live_gateway, keys, LEAK)

    assert status == 200  # the headers were long gone
    assert LEAKED_KEY not in received
    assert "response_blocked" in received
    [row] = await usage_rows(sessionmaker)
    assert (row.output_verdict, row.output_action) == (FLAGGED, TRUNCATED)
    assert (row.error_source, row.error_code) == ("gateway", "response_blocked")


async def test_a_clean_stream_is_delivered_complete_with_the_window_in_place(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The bytes held back at the end are released once the stream finishes cleanly. Getting
    this wrong truncates every response in block mode, which is why it has its own test."""
    await set_mode(sessionmaker, MODE_BLOCK)

    status, received = await read_stream(live_gateway, keys, "what is a reverse proxy?")

    assert status == 200
    events = [line for line in received.splitlines() if line.startswith("data:")]
    assert events, received
    last = json.loads(events[-1].removeprefix("data:"))
    assert last["candidates"][0]["finishReason"] == "STOP"
    [row] = await usage_rows(sessionmaker)
    assert (row.output_verdict, row.output_action) == (CLEAN, ALLOWED)
    assert row.error_code is None


async def test_a_leaking_response_does_not_leak_into_telemetry(
    gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    spans: Any,
    meter: Any,
    log_lines: list[str],
) -> None:
    """The telemetry canary, pointed at the one component whose job is to read responses. A
    scanner that logged what it matched in order to be helpful would be writing live
    credentials into a third party's trace storage."""
    await set_mode(sessionmaker, MODE_BLOCK)

    response = await gateway.post(URL, json=body(LEAK), headers=auth(keys.live))

    assert response.status_code == 403  # it really was scanned and withheld
    haystack = telemetry_text(spans, meter, log_lines)
    assert LEAKED_KEY not in haystack
    assert "aws_access_key_id" in haystack  # the rule id is what travels instead
