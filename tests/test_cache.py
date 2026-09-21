"""The response cache: what counts as the same request, and what a hit is allowed to do.

The tests are in two halves. The first half is about the key and needs no database: it
pins the normalisation rules one at a time, because each of them is a decision that could
plausibly have been made the other way and would then show up only as one tenant being
handed the answer to a different question. The second half drives the gateway and checks
that a hit is genuinely free, genuinely scoped, and genuinely indistinguishable on the wire.
"""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mock_upstream import main as mock
from tests.conftest import Keys, make_settings, usage_rows
from tollgate.auth import generate_key
from tollgate.cache.exact import CachedResponse
from tollgate.cache.keys import (
    BYPASS,
    EXACT_HIT,
    MISS,
    PROVIDER_DEFAULT_TEMPERATURE,
    cache_key,
    query_material,
    request_block_reason,
    response_block_reason,
)
from tollgate.cache.replay import assemble, events_for, stream_events, token_counts
from tollgate.config import Settings, get_settings
from tollgate.db.models import ApiKey, CacheEntry, Tenant

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent"
Sessionmaker = async_sessionmaker[AsyncSession]

# Every request in this file names a temperature, because the shipped default caches
# nothing that does not. test_a_request_without_a_temperature_is_bypassed pins that
# default; everything else here is about what happens once a request is eligible.
ZERO_TEMPERATURE: dict[str, Any] = {"generationConfig": {"temperature": 0}}


def body(text: str = "what is a reverse proxy?", **extra: Any) -> dict[str, Any]:
    return {"contents": [{"role": "user", "parts": [{"text": text}]}], **ZERO_TEMPERATURE, **extra}


def config(**fields: Any) -> dict[str, Any]:
    """A body whose generationConfig is replaced wholesale rather than merged."""
    return {
        "contents": [{"role": "user", "parts": [{"text": "what is a reverse proxy?"}]}],
        "generationConfig": fields,
    }


def auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


@pytest.fixture
def cache_settings() -> dict[str, Any]:
    """On. The suite's default is off; the fixture in conftest says why."""
    return {"cache_enabled": True}


@pytest.fixture
def upstream_transport() -> None:
    """Reach the mock over loopback rather than in process, for the whole module.

    httpx's ASGI transport collects a whole response before returning it, so with it in
    place a replayed stream would arrive as one lump and the test asserting that it did
    not would pass without anything ever having streamed.
    """
    return None


@pytest.fixture
def settings(live_upstream: str) -> Settings:
    return make_settings().model_copy(update={"upstream_base_url": live_upstream})


def key_for(
    payload: dict[str, Any],
    *,
    tenant: uuid.UUID | None = None,
    model: str = MODEL,
    query: str = "",
) -> str:
    computed = cache_key(
        tenant_id=tenant or uuid.UUID(int=1), model=model, payload=payload, query=query
    )
    assert computed is not None
    return computed


# =========================================================================================
# The key
# =========================================================================================


def test_key_ignores_the_order_object_members_were_written_in() -> None:
    """JSON objects are unordered by definition, so two encoders may disagree."""
    assert key_for({"a": 1, "b": {"x": 1, "y": 2}}) == key_for({"b": {"y": 2, "x": 1}, "a": 1})


def test_key_treats_zero_and_zero_point_zero_as_one_temperature() -> None:
    """Which of the two a client sends is its JSON encoder's choice, not the caller's."""
    assert key_for(config(temperature=0)) == key_for(config(temperature=0.0))


def test_key_does_not_normalise_the_prompt_itself() -> None:
    """The tempting bug. Trimming or lower-casing prompt text would lift the hit rate,
    and would be the gateway deciding on the model's behalf that two different inputs
    are one - a decision a gateway has no standing to make."""
    assert key_for(body("Hello")) != key_for(body("hello"))
    assert key_for(body("Hello")) != key_for(body("Hello "))
    assert key_for(body("Hello")) != key_for(body("Hello\n"))


@pytest.mark.parametrize(
    ("description", "changed"),
    [
        ("a different prompt", body("what is a forward proxy?")),
        ("a different temperature", config(temperature=0.5)),
        ("a token ceiling", config(temperature=0, maxOutputTokens=64)),
        ("a response format", config(temperature=0, responseMimeType="application/json")),
        ("a system instruction", body(systemInstruction={"parts": [{"text": "be terse"}]})),
        ("a tool declaration", body(tools=[{"functionDeclarations": [{"name": "search"}]}])),
        ("a safety setting", body(safetySettings=[{"category": "HARM_CATEGORY_HARASSMENT"}])),
        ("a field this gateway has never heard of", body(someFutureKnob={"on": True})),
    ],
)
def test_key_changes_when_anything_that_could_change_the_answer_changes(
    description: str, changed: dict[str, Any]
) -> None:
    """The last case is the one that argues for a deny-list. A field nobody has taught
    this gateway about still has to change the key, because assuming it is inert is
    precisely how an allow-list produces a wrong hit."""
    assert key_for(body()) != key_for(changed), description


def test_key_depends_on_the_order_tools_are_declared_in() -> None:
    """Object keys may be sorted; arrays may not. The order tool declarations arrive in
    is part of the prompt the model is shown."""
    first = body(tools=[{"functionDeclarations": [{"name": "a"}, {"name": "b"}]}])
    second = body(tools=[{"functionDeclarations": [{"name": "b"}, {"name": "a"}]}])
    assert key_for(first) != key_for(second)


def test_key_is_scoped_to_the_tenant_and_the_model() -> None:
    assert key_for(body(), tenant=uuid.UUID(int=1)) != key_for(body(), tenant=uuid.UUID(int=2))
    assert key_for(body(), model=MODEL) != key_for(body(), model="gemini-3.7-pro")


def test_key_ignores_the_parameters_that_only_pick_a_transport() -> None:
    """`alt=sse` chooses the framing and `key` is the caller's own credential. Neither
    changes a token, so one entry serves a streaming caller and a unary one alike."""
    assert query_material([("alt", "sse")]) == query_material([])
    assert query_material([("key", "tg_secret")]) == query_material([])


def test_key_keeps_a_query_parameter_it_does_not_recognise() -> None:
    assert query_material([("someFutureFlag", "1")]) != query_material([])


def test_key_is_not_confused_by_a_forged_field_boundary() -> None:
    """Joining fields with a separator they might contain is how two different requests
    become one string. Model "a" with query "b", and model "a\\x1fb" with no query, would
    hash identical material if the boundary could be forged."""
    assert key_for(body(), model="a", query="b") != key_for(body(), model="a\x1fb", query="")


def test_a_body_that_has_no_canonical_form_has_no_key() -> None:
    """json.loads accepts NaN, which has no canonical form and no equality worth
    building a cache key on."""
    assert (
        cache_key(tenant_id=uuid.UUID(int=1), model=MODEL, payload={"x": float("nan")}, query="")
        is None
    )


# =========================================================================================
# What may be cached
# =========================================================================================


def block(payload: dict[str, Any] | None, *, ceiling: float = 0.0) -> str | None:
    return request_block_reason(
        payload, max_temperature=ceiling, assumed_temperature=PROVIDER_DEFAULT_TEMPERATURE
    )


def test_a_request_without_a_temperature_is_bypassed() -> None:
    """Gemini samples at 1.0 when asked for nothing, so an absent temperature is not a
    zero. Reading it as one would cache sampled output under a ceiling written to forbid
    exactly that."""
    assert block({"contents": []}) == "temperature_above_ceiling"


def test_a_deterministic_request_is_eligible() -> None:
    assert block(body()) is None


def test_a_sampled_request_is_bypassed_and_says_why() -> None:
    assert block(config(temperature=0.7)) == "temperature_above_ceiling"


def test_the_ceiling_is_a_ceiling_and_not_an_equality() -> None:
    assert block(config(temperature=0.7), ceiling=0.7) is None
    assert block(config(temperature=0.8), ceiling=0.7) == "temperature_above_ceiling"


def test_asking_for_several_candidates_is_asking_for_variety() -> None:
    assert block(config(temperature=0, candidateCount=2)) == "multiple_candidates"


def test_an_unparseable_body_is_bypassed_rather_than_guessed_at() -> None:
    assert block(None) == "unparseable_body"


@pytest.mark.parametrize(
    ("description", "response", "expected"),
    [
        ("an error inside a 200", {"error": {"status": "INTERNAL"}}, "error_in_body"),
        ("a blocked prompt", {"promptFeedback": {"blockReason": "SAFETY"}}, "no_candidates"),
        (
            "a safety stop",
            {"candidates": [{"finishReason": "SAFETY"}], "usageMetadata": {}},
            "finish_reason_safety",
        ),
        (
            "a truncated answer",
            {"candidates": [{"finishReason": "MAX_TOKENS"}], "usageMetadata": {}},
            "finish_reason_max_tokens",
        ),
        (
            "nothing to price it by",
            {"candidates": [{"finishReason": "STOP"}]},
            "no_usage_metadata",
        ),
    ],
)
def test_responses_that_must_not_be_stored(
    description: str, response: dict[str, Any], expected: str
) -> None:
    """The safety stop is the one worth naming. It is a policy decision the provider
    revisits, and storing one is how a product acquires a refusal nobody can clear."""
    assert response_block_reason(response) == expected, description


def test_a_complete_answer_may_be_stored() -> None:
    assert (
        response_block_reason(
            {
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
            }
        )
        is None
    )


# =========================================================================================
# Replay, as a round trip
# =========================================================================================


STORED: dict[str, Any] = {
    "candidates": [
        {
            "content": {
                "parts": [{"text": "one two three four five six seven eight nine ten"}],
                "role": "model",
            },
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 11, "totalTokenCount": 18},
    "modelVersion": MODEL,
    "responseId": "abc123",
}


@pytest.fixture
def entry() -> CachedResponse:
    return CachedResponse(
        entry_id=uuid.uuid4(),
        response=STORED,
        input_tokens=7,
        output_tokens=11,
        thoughts_tokens=0,
        created_at=datetime.now(UTC),
    )


def test_a_response_split_into_events_and_reassembled_is_the_same_response() -> None:
    """The round trip is what lets one entry serve both methods."""
    assert assemble(list(events_for(STORED))) == STORED


def test_a_replayed_stream_is_more_than_one_event_and_ends_with_the_exact_counts() -> None:
    events = list(events_for(STORED))

    assert len(events) > 1, "a replay that is one event is not a stream"
    assert events[-1]["usageMetadata"] == STORED["usageMetadata"]
    assert events[-1]["candidates"][0]["finishReason"] == "STOP"
    # Not on an earlier one: a finish reason arriving while more text is still to come
    # tells the client the answer is over when it is not.
    assert "finishReason" not in events[0]["candidates"][0]
    # Running totals, as a live stream reports them.
    assert events[0]["usageMetadata"]["candidatesTokenCount"] < 11


def test_a_replayed_stream_carries_the_responseid_it_was_generated_under() -> None:
    assert {event["responseId"] for event in events_for(STORED)} == {"abc123"}


def test_replay_frames_both_shapes_the_way_the_provider_does(entry: CachedResponse) -> None:
    text = b"".join(stream_events(entry, sse=True)).decode()
    assert text.startswith("data: ") and text.endswith("\r\n\r\n")

    document = b"".join(stream_events(entry, sse=False)).decode()
    parsed = json.loads(document)  # a streamed JSON array is still a JSON array
    assert isinstance(parsed, list) and len(parsed) > 1


def test_token_counts_come_off_the_usage_block() -> None:
    assert token_counts(STORED) == (7, 11, 0)
    assert token_counts({}) == (0, 0, 0)


# =========================================================================================
# Through the gateway
# =========================================================================================


def upstream_calls(method: str = "generateContent") -> int:
    return mock.stats.calls[method]


async def test_the_second_identical_request_never_reaches_the_provider(
    live_gateway: httpx.AsyncClient, keys: Keys
) -> None:
    first = await live_gateway.post(URL, json=body(), headers=auth(keys.live))
    after_first = upstream_calls()
    second = await live_gateway.post(URL, json=body(), headers=auth(keys.live))

    assert first.status_code == second.status_code == 200
    assert upstream_calls() == after_first, "the provider was called twice"
    assert first.json() == second.json()
    # The bodies are byte-identical, so the header is the only way to tell them apart.
    assert first.headers["x-tollgate-cache"] == MISS
    assert second.headers["x-tollgate-cache"] == EXACT_HIT


async def test_a_hit_is_free_and_records_what_it_saved(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await live_gateway.post(URL, json=body(), headers=auth(keys.live))
    await live_gateway.post(URL, json=body(), headers=auth(keys.live))

    miss, hit = sorted(await usage_rows(sessionmaker), key=lambda row: row.created_at)
    assert (miss.cache_status, hit.cache_status) == (MISS, EXACT_HIT)

    assert miss.cost_microcents is not None and miss.cost_microcents > 0
    assert miss.cost_avoided_microcents is None

    # The tenant is not charged twice for one generation, and what the call would have
    # cost is recorded beside the zero rather than folded into it.
    assert hit.cost_microcents == 0
    assert hit.cost_avoided_microcents == miss.cost_microcents
    # No upstream call was made, so there is no upstream latency to report.
    assert hit.upstream_latency_ms is None
    assert hit.upstream_attempts == 0
    # The counts are the ones the provider reported when the answer was generated.
    assert (hit.input_tokens, hit.output_tokens) == (miss.input_tokens, miss.output_tokens)


async def test_a_hit_is_much_faster_than_the_call_it_replaces(
    live_gateway: httpx.AsyncClient, keys: Keys
) -> None:
    """The point of the phase, against an upstream that has been told to be slow."""
    slow = body("[[mock:slow=400]] summarise the gateway")

    start = time.perf_counter()
    await live_gateway.post(URL, json=slow, headers=auth(keys.live))
    miss_ms = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    response = await live_gateway.post(URL, json=slow, headers=auth(keys.live))
    hit_ms = (time.perf_counter() - start) * 1000

    assert response.headers["x-tollgate-cache"] == EXACT_HIT
    assert miss_ms > 350, f"the upstream was meant to be slow; the miss took {miss_ms:.0f} ms"
    assert hit_ms < miss_ms / 4, f"hit {hit_ms:.0f} ms against a miss of {miss_ms:.0f} ms"


async def test_one_tenant_cannot_be_served_another_tenants_answer(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys, second_tenant: str
) -> None:
    """The invariant the whole scoping design exists for. Both tenants ask the same
    question in the same words, and the second must still reach the provider."""
    await live_gateway.post(URL, json=body(), headers=auth(keys.live))
    after_first = upstream_calls()

    response = await live_gateway.post(URL, json=body(), headers=auth(second_tenant))

    assert response.status_code == 200
    assert upstream_calls() == after_first + 1, "a tenant was served another tenant's entry"
    assert response.headers["x-tollgate-cache"] == MISS

    async with sessionmaker() as session:
        entries = list((await session.scalars(select(CacheEntry))).all())
    assert {entry.tenant_id for entry in entries} != set(), "nothing was stored at all"
    assert len({entry.tenant_id for entry in entries}) == 2
    # Two tenants, one question, two different keys: the tenant id is inside the hash as
    # well as in the WHERE clause, so a query that forgot the scope still could not
    # bring these two together.
    assert len({entry.cache_key for entry in entries}) == 2


async def test_a_sampled_request_never_touches_the_cache(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    sampled = config(temperature=1.0)
    await live_gateway.post(URL, json=sampled, headers=auth(keys.live))
    after_first = upstream_calls()
    await live_gateway.post(URL, json=sampled, headers=auth(keys.live))

    assert upstream_calls() == after_first + 1
    assert [row.cache_status for row in await usage_rows(sessionmaker)] == [BYPASS, BYPASS]
    async with sessionmaker() as session:
        assert list((await session.scalars(select(CacheEntry))).all()) == []


async def test_an_upstream_error_is_not_kept(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    failing = body("[[mock:error=500]] hello")
    first = await live_gateway.post(URL, json=failing, headers=auth(keys.live))
    second = await live_gateway.post(URL, json=failing, headers=auth(keys.live))

    assert first.status_code == second.status_code == 500
    async with sessionmaker() as session:
        assert list((await session.scalars(select(CacheEntry))).all()) == []


async def test_an_expired_entry_is_not_served(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The expiry is evaluated by Postgres, so one clock decides for every container."""
    await live_gateway.post(URL, json=body(), headers=auth(keys.live))
    async with sessionmaker() as session:
        stored = (await session.scalars(select(CacheEntry))).one()
        stored.expires_at = stored.created_at
        await session.commit()

    after_first = upstream_calls()
    response = await live_gateway.post(URL, json=body(), headers=auth(keys.live))

    assert upstream_calls() == after_first + 1
    assert response.headers["x-tollgate-cache"] == MISS


class TestSwitchedOff:
    """The cache off, which is a different state from the cache declining a request."""

    @pytest.fixture
    def cache_settings(self) -> dict[str, Any]:
        return {"cache_enabled": False}

    async def test_a_cache_switched_off_leaves_no_trace_on_the_ledger(
        self, live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
    ) -> None:
        """NULL is not "miss". A request the cache never saw and one it turned away are
        different facts, and a hit rate over them would mean two different things."""
        await live_gateway.post(URL, json=body(), headers=auth(keys.live))
        [row] = await usage_rows(sessionmaker)
        assert row.cache_status is None

    async def test_nothing_is_stored_while_it_is_off(
        self, live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
    ) -> None:
        await live_gateway.post(URL, json=body(), headers=auth(keys.live))
        async with sessionmaker() as session:
            assert list((await session.scalars(select(CacheEntry))).all()) == []


# --- streaming -----------------------------------------------------------------------------


def sse_events(chunks: list[bytes]) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in b"".join(chunks).decode().splitlines()
        if line.startswith("data: ")
    ]


def text_of(events: list[dict[str, Any]]) -> str:
    return "".join(
        part["text"]
        for event in events
        for part in event["candidates"][0]["content"]["parts"]
        if "text" in part
    )


async def test_a_cached_streaming_request_still_streams(
    live_gateway: httpx.AsyncClient, keys: Keys
) -> None:
    """A hit has to be a stream, not one lump wearing a stream's content type."""
    await live_gateway.post(f"{STREAM_URL}?alt=sse", json=body(), headers=auth(keys.live))

    chunks: list[bytes] = []
    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=body(), headers=auth(keys.live)
    ) as response:
        assert response.headers["x-tollgate-cache"] == EXACT_HIT
        assert response.headers["content-type"].startswith("text/event-stream")
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    events = sse_events(chunks)
    assert len(events) > 1, "a replayed stream arrived as a single event"
    assert events[-1]["candidates"][0]["finishReason"] == "STOP"
    assert events[-1]["usageMetadata"]["candidatesTokenCount"] > 0


async def test_an_entry_a_stream_paid_for_answers_a_unary_caller(
    live_gateway: httpx.AsyncClient, keys: Keys
) -> None:
    """The method is not part of the key, which is only safe because a replay can
    produce either shape. This is the test that holds that decision up."""
    chunks: list[bytes] = []
    async with live_gateway.stream(
        "POST", f"{STREAM_URL}?alt=sse", json=body(), headers=auth(keys.live)
    ) as response:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    before = upstream_calls("generateContent")
    unary = await live_gateway.post(URL, json=body(), headers=auth(keys.live))

    assert unary.headers["x-tollgate-cache"] == EXACT_HIT
    assert upstream_calls("generateContent") == before
    # And the answer it serves is the text the streaming caller actually received.
    served = "".join(
        part["text"] for part in unary.json()["candidates"][0]["content"]["parts"] if "text" in part
    )
    assert served == text_of(sse_events(chunks))


async def test_a_stream_the_caller_abandoned_is_not_kept(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """Half an answer, replayed as though it were whole, is the worst thing this cache
    could do - and hanging up is the ordinary way half an answer comes about."""
    async with live_gateway.stream(
        "POST",
        f"{STREAM_URL}?alt=sse",
        json=body("[[mock:tokens=600]] hello"),
        headers=auth(keys.live),
    ) as response:
        async for _ in response.aiter_bytes():
            break  # the caller walks away mid-stream

    rows = []
    for _ in range(50):  # the row is written by a shielded background task
        rows = await usage_rows(sessionmaker)
        if rows:
            break
        await asyncio.sleep(0.05)

    [row] = rows
    assert row.client_disconnected is True
    async with sessionmaker() as session:
        assert list((await session.scalars(select(CacheEntry))).all()) == [], (
            "a partial answer was stored, and would be replayed as though it were whole"
        )


class TestOversizedResponse:
    """Phase 3 promised flat memory under a long stream. The cache keeps that promise by
    having a bound at all, so what matters is that the bound is obeyed - not how large it
    is. Shrinking it here tests the mechanism without generating a megabyte to do it."""

    # What is counted is the raw bytes arriving from the upstream, not the text in them,
    # and an event repeats its envelope - usage, model version, response id - every time.
    # A few hundred characters of answer is therefore several kilobytes on the wire, so
    # the bound here is set well clear of a short reply and well under a long one.
    BOUND = 16 * 1024

    @pytest.fixture
    def cache_settings(self) -> dict[str, Any]:
        return {"cache_enabled": True, "cache_max_response_bytes": TestOversizedResponse.BOUND}

    async def test_a_response_too_large_to_hold_is_relayed_and_not_stored(
        self, live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
    ) -> None:
        chunks: list[bytes] = []
        async with live_gateway.stream(
            "POST",
            f"{STREAM_URL}?alt=sse",
            json=body("[[mock:tokens=900]] write at length"),
            headers=auth(keys.live),
        ) as response:
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)

        received = sum(len(chunk) for chunk in chunks)
        assert received > self.BOUND, f"the test needs a response over the bound, got {received:,}"
        # Relayed in full and correctly, exactly as it would have been without a cache.
        assert sse_events(chunks)[-1]["candidates"][0]["finishReason"] == "STOP"
        async with sessionmaker() as session:
            assert list((await session.scalars(select(CacheEntry))).all()) == []

    async def test_a_response_inside_the_bound_is_still_stored(
        self, live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
    ) -> None:
        """The other side of the same bound, so a test passing above cannot be a cache
        that simply never stores a streamed response at all."""
        received = 0
        async with live_gateway.stream(
            "POST", f"{STREAM_URL}?alt=sse", json=body("hi"), headers=auth(keys.live)
        ) as response:
            async for chunk in response.aiter_bytes():
                received += len(chunk)

        assert received < self.BOUND, f"the test needs a response under the bound, got {received:,}"
        async with sessionmaker() as session:
            assert len(list((await session.scalars(select(CacheEntry))).all())) == 1


# --- metrics -------------------------------------------------------------------------------


async def test_the_cache_is_counted_by_outcome_and_by_what_it_saved(
    live_gateway: httpx.AsyncClient, keys: Keys, meter: Any
) -> None:
    await live_gateway.post(URL, json=body(), headers=auth(keys.live))
    await live_gateway.post(URL, json=body(), headers=auth(keys.live))
    await live_gateway.post(URL, json=config(temperature=1.0), headers=auth(keys.live))

    outcomes: dict[str, float] = {}
    savings = 0.0
    data = meter.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    if metric.name == "tollgate.cache.lookups":
                        result = str(point.attributes["result"])
                        outcomes[result] = outcomes.get(result, 0) + point.value
                    if metric.name == "tollgate.cache.savings":
                        savings += point.value

    # The bypass is counted too, so a hit rate has an honest denominator: a gateway whose
    # traffic is nearly all sampled should not report a flattering rate over the handful
    # of requests that were ever eligible for a hit.
    assert outcomes == {MISS: 1, EXACT_HIT: 1, BYPASS: 1}
    assert savings > 0


# --- the shipped defaults ---------------------------------------------------------------


def test_the_shipped_default_refuses_to_cache_anything_sampled() -> None:
    """Pinned apart from the fixtures, which switch the cache on in order to test it.
    What ships is the setting that cannot change a tenant's product behind its back."""
    get_settings.cache_clear()
    try:
        settings = Settings(_env_file=None)
        assert settings.cache_max_temperature == 0.0
        assert settings.cache_assumed_temperature == PROVIDER_DEFAULT_TEMPERATURE
    finally:
        get_settings.cache_clear()


# --- fixtures ------------------------------------------------------------------------------


@pytest.fixture
async def second_tenant(sessionmaker: Sessionmaker) -> str:
    """A second tenant with a live key of its own, for the scoping test."""
    new = generate_key()
    async with sessionmaker() as session:
        tenant = Tenant(name="globex", rate_limit_rpm=100_000, rate_limit_burst=100_000)
        session.add(tenant)
        await session.flush()
        session.add(ApiKey(tenant_id=tenant.id, name="a", key_prefix=new.prefix, key_hash=new.hash))
        await session.commit()
    return new.plaintext
