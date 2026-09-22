"""The semantic tier: what it is allowed to match on, and everything it must not.

An exact hit is justified by the request being the same once normalised, which is a fact
a hash settles. A semantic hit is justified by a score clearing a threshold, which is a
judgement - so most of this file is about the guards around that judgement rather than
about the judgement itself. Whether the threshold is any good is a question for
bench/cache_sweep.py, which answers it with a labelled set and a curve.

The similarity figures below come from the mock's embeddings, which hash words and
bigrams into a vector. That is enough to tell "the same sentence again" from "a different
question", which is what these tests need. It is emphatically not enough to tell a
paraphrase from a negation, and the sweep is where that is measured and reported.
"""

import json
import uuid
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mock_upstream import main as mock
from tests.conftest import Keys, make_settings, usage_rows
from tollgate.cache.keys import (
    EXACT_HIT,
    MISS,
    SEMANTIC_HIT,
    cache_key,
    params_key,
    prompt_text,
    semantic_block_reason,
)
from tollgate.cache.semantic import cosine_similarity
from tollgate.cache.service import CacheService, build_cache
from tollgate.config import Settings
from tollgate.db.models import CacheEntry

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
Sessionmaker = async_sessionmaker[AsyncSession]

# Low enough that the mock's embeddings clear it for the same sentence written two ways
# (0.83 for an added greeting), and far above an unrelated question (0.29). The number
# the gateway ships is chosen by the sweep, not by this file.
TEST_THRESHOLD = 0.75

QUESTION = "What is a reverse proxy?"
SAME_QUESTION_POLITELY = "Hi! What is a reverse proxy? Thanks!"
DIFFERENT_QUESTION = "How do histogram buckets affect a percentile?"


def body(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0},
        **extra,
    }


def auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


@pytest.fixture
def cache_settings() -> dict[str, Any]:
    return {
        "cache_enabled": True,
        "cache_semantic_enabled": True,
        "cache_semantic_threshold": TEST_THRESHOLD,
    }


@pytest.fixture
def upstream_transport() -> None:
    """Loopback rather than in-process, so the embedding call is a real round trip."""
    return None


@pytest.fixture
def settings(live_upstream: str) -> Settings:
    return make_settings().model_copy(update={"upstream_base_url": live_upstream})


def generations(method: str = "generateContent") -> int:
    return mock.stats.calls[method]


def embeddings() -> int:
    return mock.stats.calls["embedContent"]


# =========================================================================================
# What similarity is measured over
# =========================================================================================


def test_the_parameters_hash_covers_everything_except_the_prompt() -> None:
    """The guard that makes a semantic hit safe. Two requests may be compared on their
    prompts only once everything else about them is known to be identical."""
    tenant = uuid.UUID(int=1)

    def params(payload: dict[str, Any], model: str = MODEL) -> str:
        key = params_key(tenant_id=tenant, model=model, payload=payload, query="")
        assert key is not None
        return key

    # A different prompt does not change it: that is the whole point.
    assert params(body(QUESTION)) == params(body(DIFFERENT_QUESTION))

    # Everything else does.
    assert params(body(QUESTION)) != params(body(QUESTION, systemInstruction={"x": 1}))
    assert params(body(QUESTION)) != params(body(QUESTION, tools=[{"a": 1}]))
    assert params(body(QUESTION), model=MODEL) != params(body(QUESTION), model="other")
    hotter = {
        "contents": [{"role": "user", "parts": [{"text": QUESTION}]}],
        "generationConfig": {"temperature": 0.5},
    }
    assert params(body(QUESTION)) != params(hotter)


def test_the_parameters_hash_is_not_the_exact_key() -> None:
    """A request with no `contents` at all would otherwise hash the same document twice."""
    tenant, payload = uuid.UUID(int=1), {"generationConfig": {"temperature": 0}}
    assert cache_key(tenant_id=tenant, model=MODEL, payload=payload, query="") != params_key(
        tenant_id=tenant, model=MODEL, payload=payload, query=""
    )


def test_the_whole_conversation_is_embedded_and_roles_are_kept() -> None:
    """Two conversations ending in the same words are not the same question."""
    first = {
        "contents": [
            {"role": "user", "parts": [{"text": "Is Postgres a good fit?"}]},
            {"role": "model", "parts": [{"text": "Often, yes."}]},
            {"role": "user", "parts": [{"text": "And why?"}]},
        ]
    }
    second = {
        "contents": [
            {"role": "user", "parts": [{"text": "Is Redis a good fit?"}]},
            {"role": "model", "parts": [{"text": "Often, yes."}]},
            {"role": "user", "parts": [{"text": "And why?"}]},
        ]
    }
    assert prompt_text(first) != prompt_text(second)
    assert prompt_text(first).startswith("user: Is Postgres")
    assert "model: Often, yes." in prompt_text(first)


@pytest.mark.parametrize(
    ("description", "payload", "expected"),
    [
        (
            "an image the embedding cannot see",
            {
                "contents": [
                    {"role": "user", "parts": [{"text": "what is this"}, {"inlineData": {}}]}
                ]
            },
            "non_text_parts",
        ),
        (
            "a function response",
            {"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "f"}}]}]},
            "no_prompt_text",
        ),
        ("nothing to embed", {"contents": []}, "no_prompt_text"),
        ("whitespace only", {"contents": [{"parts": [{"text": "   "}]}]}, "no_prompt_text"),
    ],
)
def test_requests_the_semantic_tier_must_refuse(
    description: str, payload: dict[str, Any], expected: str
) -> None:
    """The gap that is easy to miss: `contents` is excluded from the parameters hash
    because the prompt is what similarity covers, so anything else inside `contents` is
    covered by neither. Two requests with the same text and different images would look
    like one request, which is why a conversation that is not all text is refused."""
    assert semantic_block_reason(payload) == expected, description


def test_a_text_only_conversation_is_allowed() -> None:
    assert semantic_block_reason(body(QUESTION)) is None


def test_cosine_similarity_agrees_with_the_obvious_cases() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)  # scale-free
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    with pytest.raises(ValueError):
        cosine_similarity([1.0], [1.0, 0.0])


# =========================================================================================
# Through the gateway
# =========================================================================================


async def test_a_differently_worded_question_is_answered_from_the_stored_one(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    first = await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))
    after_first = generations()

    second = await live_gateway.post(
        URL, json=body(SAME_QUESTION_POLITELY), headers=auth(keys.live)
    )

    assert first.headers["x-tollgate-cache"] == MISS
    assert second.headers["x-tollgate-cache"] == SEMANTIC_HIT
    assert generations() == after_first, "the provider was called for a question already answered"
    assert second.json() == first.json()

    miss, hit = sorted(await usage_rows(sessionmaker), key=lambda row: row.created_at)
    assert hit.cache_status == SEMANTIC_HIT
    assert hit.cache_similarity is not None and hit.cache_similarity >= TEST_THRESHOLD
    assert miss.cache_similarity is None
    # Free to the tenant, and what it would have cost recorded beside the zero.
    assert hit.cost_microcents == 0
    assert hit.cost_avoided_microcents == miss.cost_microcents


async def test_a_different_question_is_not(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))
    after_first = generations()

    response = await live_gateway.post(URL, json=body(DIFFERENT_QUESTION), headers=auth(keys.live))

    assert response.headers["x-tollgate-cache"] == MISS
    assert generations() == after_first + 1


async def test_an_identical_request_still_takes_the_exact_tier_and_pays_no_embedding(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The cheap tier runs first. An exact hit must not cost an upstream round trip to
    an embedding model on the way to an answer the gateway already had indexed."""
    await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))
    after_first = embeddings()

    response = await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))

    assert response.headers["x-tollgate-cache"] == EXACT_HIT
    assert embeddings() == after_first, "an exact hit paid for an embedding"
    _, hit = sorted(await usage_rows(sessionmaker), key=lambda row: row.created_at)
    assert hit.embedding_cost_microcents is None


async def test_the_same_prompt_under_different_parameters_is_never_matched(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The test this whole design exists for. Identical prompts, so the embeddings are
    identical and the similarity is exactly 1.0 - and the answer still must not be
    reused, because the instruction that shapes it is different."""
    terse = body(QUESTION, systemInstruction={"parts": [{"text": "Answer in one word."}]})
    verbose = body(QUESTION, systemInstruction={"parts": [{"text": "Answer at length."}]})

    await live_gateway.post(URL, json=terse, headers=auth(keys.live))
    after_first = generations()
    response = await live_gateway.post(URL, json=verbose, headers=auth(keys.live))

    assert response.headers["x-tollgate-cache"] == MISS
    assert generations() == after_first + 1
    async with sessionmaker() as session:
        entries = list((await session.scalars(select(CacheEntry))).all())
    assert len({entry.params_key for entry in entries}) == 2


async def test_one_tenant_is_never_served_another_tenants_similar_answer(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys, second_tenant: str
) -> None:
    await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))
    after_first = generations()

    response = await live_gateway.post(
        URL, json=body(SAME_QUESTION_POLITELY), headers=auth(second_tenant)
    )

    assert response.headers["x-tollgate-cache"] == MISS
    assert generations() == after_first + 1


async def test_what_the_search_cost_is_recorded_even_when_it_finds_nothing(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The number that decides whether the tier is worth running. A miss under it pays
    for an embedding that found nothing, and that spend has to be visible or the saving
    beside it is only half the story."""
    await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))

    [row] = await usage_rows(sessionmaker)
    assert row.cache_status == MISS
    assert row.embedding_cost_microcents is not None and row.embedding_cost_microcents > 0
    # Its own column, never folded into what the tenant is charged for generation.
    assert row.cost_microcents is not None and row.cost_microcents > 0
    assert row.cost_microcents != row.embedding_cost_microcents


async def test_the_embedding_wait_is_upstream_time_and_not_gateway_overhead(
    live_gateway: httpx.AsyncClient, keys: Keys, meter: Any
) -> None:
    """The tier's own round trip must not be charged to the gateway.

    An embedding call waits on the provider exactly as a generation call does, but it lives
    in a different span - so unless it is added in, `total - upstream` files it as overhead.
    That would make enabling a cache look like a latency regression in the service and put
    the overhead alert permanently in alarm.

    The mock holds a steady 100 ms, so a semantic miss waits on it twice.
    """
    await live_gateway.post(
        URL, json=body("[[mock:slow=300]] " + QUESTION), headers=auth(keys.live)
    )

    recorded: dict[str, list[Any]] = {}
    data = meter.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                recorded.setdefault(metric.name, []).extend(metric.data.data_points)

    [total] = recorded["tollgate.request.duration"]
    [upstream] = recorded["tollgate.upstream.duration"]
    [overhead] = recorded["tollgate.overhead"]

    # Both 300 ms waits are in the upstream figure, so it is over 550 even allowing for
    # the clocks being taken at slightly different points.
    assert upstream.sum > 550, f"an embedding wait is missing from upstream: {upstream.sum:.0f}ms"
    assert total.sum >= upstream.sum

    # The bound is the length of one provider wait, and that is the whole argument: if
    # either round trip were being charged to the gateway, overhead would be at least
    # 300 ms rather than comfortably under it. It is not a budget for the gateway's own
    # work, which in this harness is tens of milliseconds of its own - every session
    # opens a fresh connection, because the test engine uses NullPool - and which a
    # benchmark rather than a test is the right place to measure.
    assert overhead.sum < 300, f"a provider wait was charged as overhead: {overhead.sum:.0f}ms"


async def test_a_stored_entry_carries_its_vector_and_its_parameters(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))

    async with sessionmaker() as session:
        entry = (await session.scalars(select(CacheEntry))).one()
    assert entry.params_key is not None
    assert entry.embedding is not None
    assert len(entry.embedding) == 768


async def test_an_unreachable_embedding_model_is_a_miss_and_not_an_error(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys, live_upstream: str
) -> None:
    """The tier is an optimisation. Losing it costs money and never correctness, so the
    caller gets the answer it would have got without the tier existing."""
    await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))

    # A prompt the mock refuses to embed: an error directive travels in the text.
    failing = body("[[mock:error=503]] " + SAME_QUESTION_POLITELY)
    response = await live_gateway.post(URL, json=failing, headers=auth(keys.live))

    assert response.status_code == 503  # the generation call failed, as the directive asks
    assert response.headers["x-tollgate-cache"] == MISS


class TestTierOff:
    """The exact tier alone, which is what ships."""

    @pytest.fixture
    def cache_settings(self) -> dict[str, Any]:
        return {"cache_enabled": True, "cache_semantic_enabled": False}

    async def test_nothing_is_embedded(
        self, live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
    ) -> None:
        before = embeddings()
        await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))
        response = await live_gateway.post(
            URL, json=body(SAME_QUESTION_POLITELY), headers=auth(keys.live)
        )

        assert embeddings() == before, "the semantic tier ran while switched off"
        assert response.headers["x-tollgate-cache"] == MISS
        rows = await usage_rows(sessionmaker)
        assert all(row.embedding_cost_microcents is None for row in rows)

    async def test_entries_written_while_off_carry_no_vector(
        self, live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
    ) -> None:
        """And are therefore invisible to the search rather than matched on partial
        information: an entry with a vector but no parameters hash could be matched on
        its prompt alone, which is the one thing the tier must never do."""
        await live_gateway.post(URL, json=body(QUESTION), headers=auth(keys.live))
        async with sessionmaker() as session:
            entry = (await session.scalars(select(CacheEntry))).one()
        assert entry.embedding is None
        assert entry.params_key is None


def test_the_gateway_ships_with_the_tier_off_and_no_threshold() -> None:
    """Both halves matter. Off, because the tier is a correctness risk nobody has
    measured for the operator's embedding model; and with no threshold, because there is
    no value that is right for more than one model. See the test below for the evidence."""
    settings = Settings(_env_file=None)
    assert settings.cache_semantic_enabled is False
    assert settings.cache_semantic_threshold is None


def test_turning_the_tier_on_without_a_threshold_refuses_to_start() -> None:
    """A plausible default is how a correctness bug gets configured in by somebody who
    never knew there was a choice. Refusing to boot is the honest alternative, and it
    points at the command that produces the number."""
    settings = Settings(_env_file=None).model_copy(update={"cache_semantic_enabled": True})

    with pytest.raises(RuntimeError, match="CACHE_SEMANTIC_THRESHOLD"):
        build_cache(settings, cast(Any, None), cast(Any, object()))


def test_a_service_cannot_hold_an_embedder_without_a_threshold() -> None:
    """The same invariant one level down, so a service assembled directly cannot end up
    searching with nothing to compare the results against."""
    with pytest.raises(ValueError, match="threshold"):
        CacheService(
            cast(Any, None),
            enabled=True,
            ttl_s=60,
            max_temperature=0.0,
            assumed_temperature=1.0,
            max_response_bytes=1024,
            embedder=cast(Any, object()),
            semantic_threshold=None,
        )


def test_the_two_sweeps_disagree_which_is_why_there_is_no_default() -> None:
    """The evidence behind the decision, pinned so it cannot quietly stop being true.

    The mock's feature hashing finds a threshold with no false hits. Gemini's embeddings
    find none at all, because "Convert JSON to YAML" and "Convert YAML to JSON" score
    higher together than any real paraphrase in the set. One number cannot serve both,
    so the gateway ships with neither.
    """
    from tests.conftest import ROOT

    results = ROOT / "bench" / "results"
    mock_sweep = (results / "cache_sweep.txt").read_text(encoding="utf-8")
    gemini_sweep = (results / "cache_sweep_gemini.txt").read_text(encoding="utf-8")

    assert "CACHE_SEMANTIC_THRESHOLD=" in mock_sweep, "the mock sweep found a threshold"
    assert "NO USABLE THRESHOLD" in gemini_sweep, "the gemini sweep found none"
    assert "gemini-embedding-001" in gemini_sweep


@pytest.fixture
async def second_tenant(sessionmaker: Sessionmaker) -> str:
    from tollgate.auth import generate_key
    from tollgate.db.models import ApiKey, Tenant

    new = generate_key()
    async with sessionmaker() as session:
        tenant = Tenant(name="globex", rate_limit_rpm=100_000, rate_limit_burst=100_000)
        session.add(tenant)
        await session.flush()
        session.add(ApiKey(tenant_id=tenant.id, name="a", key_prefix=new.prefix, key_hash=new.hash))
        await session.commit()
    return new.plaintext


def test_the_labelled_set_is_hard_enough_to_be_worth_sweeping() -> None:
    """A pair set of obviously unrelated sentences would certify any threshold at all.
    The negatives have to be pairs that share almost every word and mean different
    things, or the curve is measuring nothing."""
    from tests.conftest import ROOT

    pairs = json.loads((ROOT / "bench" / "data" / "prompt_pairs.json").read_text(encoding="utf-8"))[
        "pairs"
    ]
    negatives = [pair for pair in pairs if not pair["same"]]
    positives = [pair for pair in pairs if pair["same"]]

    assert len(positives) >= 20 and len(negatives) >= 20
    hard = {"negation", "entity_swap", "number_swap", "direction_swap", "near_topic"}
    assert hard <= {pair["kind"] for pair in negatives}
    # The interesting negatives share most of their words with their partner.
    for pair in negatives:
        if pair["kind"] in hard:
            words_a, words_b = set(pair["a"].lower().split()), set(pair["b"].lower().split())
            overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
            assert overlap >= 0.4, f"{pair['kind']} pair is not a hard negative: {pair}"
