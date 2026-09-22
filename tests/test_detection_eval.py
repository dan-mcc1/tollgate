"""The regression gate: the eval, run in CI, with floors under it.

A benchmark that is only ever run by hand is a benchmark that quietly stops being true. These
tests re-run bench/detection_eval.py's arithmetic over the committed corpus on every build and
fail when a detector gets worse, so "recall 0.44" in the README is a claim CI is defending
rather than a number somebody measured once in September.

**The floors are deliberately below what is measured today.** A gate set at the current
figure fails on noise - one reworded rule, one extra case in the corpus - and a gate that
fails for no reason gets deleted within a month. They sit far enough below to catch a real
regression (a rule that stopped compiling, a model that loads but answers nothing, a
threshold typo) and not a rounding difference. When a change improves a number, the floor
moves up with it in the same commit, on purpose and visibly.

**The classifier's floors only run where a model is present.** The suite must work with no
model and no network, so those tests skip themselves when a graph is missing, and CI fetches
the pinned small model into a cache before the tests start.

**And only for the models named in `EVAL_MODELS`.** One pass of the large model over this
corpus is minutes, which does not belong in a suite that otherwise runs in about a minute -
a slow suite is a suite people stop running. The default is the model the image ships. Before
publishing a number, run the lot:

    EVAL_MODELS=tiny,deberta-base uv run pytest tests/test_detection_eval.py
"""

import os

import pytest

from bench.detection_eval import (
    ADJACENT_SOURCE,
    Case,
    Scored,
    baseline_result,
    classifier_result,
    combined_result,
    load_classifier,
    load_corpus,
    score_corpus,
)
from tollgate.detect.classifier import KNOWN_MODELS

# Which models this run is willing to spend time on. See the module docstring.
GATED_MODELS = {name.strip() for name in os.environ.get("EVAL_MODELS", "tiny").split(",")}

# What the committed corpus should look like. A corpus that silently shrank - a failed fetch,
# a bad merge - would make every rate below meaningless while still passing.
MIN_CASES = 1000
MIN_POSITIVES = 400
MIN_ADJACENT = 60

# The regex baseline, measured at 0.374 recall, 0.908 precision and 0.027 FPR on this corpus.
BASELINE_MIN_RECALL = 0.33
BASELINE_MAX_FPR = 0.06
BASELINE_MIN_PRECISION = 0.85
# And measured at zero on the ordinary short application prompts. This is what ships, so this is
# the floor that matters most: a rule that starts flagging "Summarise this." is a rule that makes
# the whole feature unusable, and it would not move any of the rates above enough to notice.
BASELINE_MAX_APP_FPR = 0.05

# Each classifier, at its own registry threshold. One pair per model, because these are
# properties of the model rather than of the gateway.
CLASSIFIER_FLOORS = {
    # Measured at 0.658 recall, 0.768 precision, 0.143 FPR - and 0.429 on the app prompts, which
    # is why this one is measured and not shipped.
    "tiny": (0.58, 0.20),
    # Measured at 0.666 recall, 0.938 precision, 0.032 FPR, and zero on the app prompts.
    "deberta-base": (0.60, 0.06),
}


@pytest.fixture(scope="module")
def corpus() -> list[Case]:
    return load_corpus()


@pytest.fixture(scope="module")
def baseline_scores(corpus: list[Case]) -> list[Scored]:
    return score_corpus(corpus, None)


@pytest.fixture(scope="module")
def scored_by_model(corpus: list[Case]) -> dict[str, list[Scored]]:
    """One pass of each gated model over the corpus, shared by every test below.

    Module scoped because scoring is the expensive part and the tests differ only in what
    they conclude from it - the same reason the eval itself scores once and derives.
    """
    scored: dict[str, list[Scored]] = {}
    for name in sorted(KNOWN_MODELS):
        if name not in GATED_MODELS:
            continue
        classifier = load_classifier(name)
        if classifier is not None:
            scored[name] = score_corpus(corpus, classifier)
    return scored


def scores_for(name: str, scored_by_model: dict[str, list[Scored]]) -> list[Scored]:
    if name not in GATED_MODELS:
        pytest.skip(f"{name} is not in EVAL_MODELS={','.join(sorted(GATED_MODELS))}")
    if name not in scored_by_model:
        pytest.skip(f"{name} not fetched; run: uv run python -m tollgate.detect.fetch {name}")
    return scored_by_model[name]


def test_the_committed_corpus_is_still_a_corpus(corpus: list[Case]) -> None:
    positives = [case for case in corpus if case.positive]
    adjacent = [case for case in corpus if case.source == ADJACENT_SOURCE]

    assert len(corpus) >= MIN_CASES
    assert len(positives) >= MIN_POSITIVES
    assert len(adjacent) >= MIN_ADJACENT
    assert len({case.source for case in corpus}) >= 3


def test_the_baseline_still_catches_what_it_used_to(baseline_scores: list[Scored]) -> None:
    """The regression this exists for: a rule reworded into one that compiles and matches
    nothing. Every individual rule has a unit test in tests/test_detection.py; this is the
    one that notices the whole set getting quietly worse."""
    result = baseline_result(baseline_scores)

    assert result.recall >= BASELINE_MIN_RECALL, f"recall regressed to {result.recall:.3f}"
    assert result.false_positive_rate <= BASELINE_MAX_FPR, (
        f"false positives rose to {result.false_positive_rate:.3f}"
    )


def test_the_baseline_leaves_ordinary_short_prompts_alone(baseline_scores: list[Scored]) -> None:
    """The measurement that decided what ships. `tiny` flags 43% of these - "Summarise this.",
    "Fix this SQL.", "What is a reverse proxy?" - which is why it is in the repository as evidence
    rather than in the image. The baseline flags none of them, and has to keep doing so."""
    result = baseline_result(baseline_scores)

    assert result.app_rate <= BASELINE_MAX_APP_FPR, (
        f"the baseline now flags {result.app_rate:.1%} of ordinary application prompts"
    )


def test_the_baseline_is_not_flagging_everything(baseline_scores: list[Scored]) -> None:
    """The cheap way to pass a recall gate is to flag every request. Precision is what makes
    that impossible, and it is the number a tenant in block mode actually feels."""
    result = baseline_result(baseline_scores)

    assert result.precision >= BASELINE_MIN_PRECISION


@pytest.mark.parametrize("name", sorted(KNOWN_MODELS))
def test_the_classifier_still_beats_the_baseline_on_recall(
    name: str, scored_by_model: dict[str, list[Scored]]
) -> None:
    """The claim the classifier tier exists to make good on. If a repinned model, a swapped
    label order or a wrong threshold made it worse than twenty lines of regex, there would be
    no reason to ship 250 MB of ONNX Runtime."""
    scored = scores_for(name, scored_by_model)
    baseline = baseline_result(scored)
    result = classifier_result(name, scored, KNOWN_MODELS[name].default_threshold)
    floor_recall, ceiling_fpr = CLASSIFIER_FLOORS[name]

    assert result.recall > baseline.recall, "the classifier no longer beats the regex baseline"
    assert result.recall >= floor_recall, f"recall regressed to {result.recall:.3f}"
    assert result.false_positive_rate <= ceiling_fpr, (
        f"false positives rose to {result.false_positive_rate:.3f}"
    )


@pytest.mark.parametrize("name", sorted(KNOWN_MODELS))
def test_the_shipped_arrangement_catches_more_than_either_tier(
    name: str, scored_by_model: dict[str, list[Scored]]
) -> None:
    """ "Flagged if either says so" has to earn its cost. It buys recall and spends precision;
    what it must never do is catch less than one of its own halves, which is what a threshold
    or ordering mistake would look like."""
    scored = scores_for(name, scored_by_model)
    threshold = KNOWN_MODELS[name].default_threshold
    baseline = baseline_result(scored)
    alone = classifier_result(name, scored, threshold)
    shipped = combined_result(name, scored, threshold)

    assert shipped.recall >= baseline.recall
    assert shipped.recall >= alone.recall
