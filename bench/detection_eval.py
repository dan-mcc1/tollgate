"""How good the detector actually is, and what each layer of it costs.

    uv run python bench/detection_eval.py                  # every detector that is present
    uv run python bench/detection_eval.py --model tiny     # just one
    uv run python bench/detection_eval.py --sweep          # thresholds, to choose one

The gateway's detection code is twenty minutes of regex and an ONNX session; the part that is
worth anything is the table underneath, because a detector without one is a claim.

**Accuracy is not reported, on purpose.** The corpus is 43% positive, so "always benign"
scores 57% and reads like a passing grade. Precision, recall and the false positive rate are
the numbers that cannot be gamed by a constant.

**Three arrangements, not one.** The regex baseline alone, each classifier alone, and the
combination the gateway actually ships - flagged if either says so. The combination is what
runs in production, and it cannot be more precise than its worse member, so quoting only the
classifier's precision would describe software nobody is running.

**The false positive rate is reported three times.** Once over all benign traffic, and once over
`security_adjacent` alone - the seventy hand-written prompts that look like attacks and are
not: a security engineer writing test cases, a developer pasting a log line that quotes an
injection, "ignore the previous draft". That second number is the one that decides whether a
tenant can be put in `block` mode, and it is the one nobody publishes.

The third is `app_prompts.jsonl`, and it separates the detectors more sharply than either: 28
prompts that are not adversarial in any way, only short - "Summarise this.", "Fix this SQL.",
"Hello.". The public benign sets are full sentences and roleplay prompts, so a classifier can
score well on them and still flag half of what a real product sends.

**Latency is measured where it is paid.** Per case, single-threaded, on the machine running
this - which is not the machine running the gateway, so the absolute numbers matter less than
the ratios between the tiers. The median and the 99th are both reported: a detector whose
median is 3 ms and whose 99th is 300 ms is a detector that will be blamed for a slow p99.
"""

import argparse
import json
import statistics
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tollgate.detect import baseline
from tollgate.detect.classifier import KNOWN_MODELS, Classifier, build_classifier

HERE = Path(__file__).parent
CORPUS = HERE / "data" / "detection_corpus.jsonl"
RESULTS = HERE / "results" / "detection_eval.txt"
MODEL_DIR = "models"
ADJACENT_SOURCE = "tollgate/security-adjacent"
APP_SOURCE = "tollgate/app-prompts"

INJECTION = "injection"


@dataclass(frozen=True)
class Case:
    text: str
    label: str
    source: str
    category: str = ""

    @property
    def positive(self) -> bool:
        return self.label == INJECTION


def load_corpus(path: Path = CORPUS) -> list[Case]:
    if not path.is_file():
        raise SystemExit(f"{path} is missing. Build it: uv run python bench/fetch_corpus.py")
    # `split("\n")` and not `splitlines()`: a JSON string may contain a raw U+2028,
    # which `splitlines()` treats as a line break and JSON does not. One prompt in this
    # corpus has one, and the corpus was unreadable until it did not.
    lines = path.read_text(encoding="utf-8").split("\n")
    return [
        Case(
            text=row["text"],
            label=row["label"],
            source=row["source"],
            category=row.get("category", ""),
        )
        for row in (json.loads(line) for line in lines if line.strip())
    ]


@dataclass
class Result:
    """One detector's answers over the whole corpus, and what they cost."""

    name: str
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    # Benign cases from each hand-written set, counted separately.
    adjacent_total: int = 0
    adjacent_flagged: int = 0
    # The ordinary short prompts an application really sends. Counted apart from everything
    # else because this is the rate that decides whether a detector can be deployed at all,
    # and no public benign set contains anything like them.
    app_total: int = 0
    app_flagged: int = 0
    # Which category of hand-written prompt it got wrong, and how often.
    adjacent_by_category: dict[str, int] = field(default_factory=dict)
    # Which baseline rule caused each false positive; empty for the classifiers.
    rules_on_false_positives: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    def record(self, case: Case, flagged: bool, elapsed_ms: float, rule: str | None = None) -> None:
        self.latencies_ms.append(elapsed_ms)
        if case.positive:
            if flagged:
                self.true_positives += 1
            else:
                self.false_negatives += 1
                self.missed.append(case.text)
            return
        if case.source == ADJACENT_SOURCE:
            self.adjacent_total += 1
            if flagged:
                self.adjacent_flagged += 1
                self.adjacent_by_category[case.category] = (
                    self.adjacent_by_category.get(case.category, 0) + 1
                )
        if case.source == APP_SOURCE:
            self.app_total += 1
            if flagged:
                self.app_flagged += 1
                self.adjacent_by_category[case.category] = (
                    self.adjacent_by_category.get(case.category, 0) + 1
                )
        if flagged:
            self.false_positives += 1
            if rule:
                self.rules_on_false_positives[rule] = self.rules_on_false_positives.get(rule, 0) + 1
        else:
            self.true_negatives += 1

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        return self.true_positives / found if found else 0.0

    @property
    def recall(self) -> float:
        real = self.true_positives + self.false_negatives
        return self.true_positives / real if real else 0.0

    @property
    def f1(self) -> float:
        if not (self.precision and self.recall):
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)

    @property
    def false_positive_rate(self) -> float:
        benign = self.false_positives + self.true_negatives
        return self.false_positives / benign if benign else 0.0

    @property
    def adjacent_rate(self) -> float:
        return self.adjacent_flagged / self.adjacent_total if self.adjacent_total else 0.0

    @property
    def app_rate(self) -> float:
        return self.app_flagged / self.app_total if self.app_total else 0.0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p99_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]


# --- running the detectors over the corpus --------------------------------------------------


@dataclass(frozen=True)
class Scored:
    """One case, as both tiers saw it.

    Every model is run over the corpus exactly once and everything else is derived from what
    it said. The obvious structure - one pass per arrangement, one more per threshold in the
    sweep - would run the expensive model five times over the same text to answer questions
    that differ only in a comparison. With the larger model at up to 300 ms a window, that is
    the difference between an eval somebody runs and an eval somebody means to run.
    """

    case: Case
    rule: str | None  # what the baseline said, or None
    score: float  # what the classifier said, or 0.0 when there is none
    baseline_ms: float
    classifier_ms: float

    @property
    def flagged_by_baseline(self) -> bool:
        return self.rule is not None


def score_corpus(cases: Iterable[Case], classifier: Classifier | None) -> list[Scored]:
    scored = []
    for case in cases:
        started = time.perf_counter()
        rule = baseline.scan(case.text)
        baseline_ms = (time.perf_counter() - started) * 1000

        score, classifier_ms = 0.0, 0.0
        if classifier is not None:
            started = time.perf_counter()
            # Folded, exactly as detect/service.py folds it before handing it over. Measuring
            # the model on text the gateway never sends it would measure something else.
            score = classifier.score_now(baseline.fold(case.text)).value
            classifier_ms = (time.perf_counter() - started) * 1000
        scored.append(Scored(case, rule, score, baseline_ms, classifier_ms))
    return scored


def baseline_result(scored: list[Scored]) -> Result:
    result = Result("regex baseline")
    for item in scored:
        result.record(item.case, item.flagged_by_baseline, item.baseline_ms, item.rule)
    return result


def classifier_result(name: str, scored: list[Scored], threshold: float) -> Result:
    result = Result(name)
    for item in scored:
        result.record(item.case, item.score >= threshold, item.classifier_ms)
    return result


def combined_result(name: str, scored: list[Scored], threshold: float) -> Result:
    """What the gateway actually runs: the baseline first, the classifier only when the
    baseline is clean, flagged if either says so.

    The latency reflects that order - a case the baseline flags never pays for inference -
    which is why the combination's median can be lower than the classifier's alone.
    """
    result = Result(name)
    for item in scored:
        if item.flagged_by_baseline:
            result.record(item.case, True, item.baseline_ms, item.rule)
            continue
        elapsed = item.baseline_ms + item.classifier_ms
        result.record(item.case, item.score >= threshold, elapsed)
    return result


# --- reporting -------------------------------------------------------------------------------


def table(results: list[Result]) -> list[str]:
    header = (
        f"{'detector':<28}{'precision':>10}{'recall':>9}{'F1':>7}"
        f"{'FPR':>8}{'FPR adj':>9}{'FPR app':>9}{'median':>9}{'p99':>9}"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        lines.append(
            f"{result.name:<28}{result.precision:>10.3f}{result.recall:>9.3f}{result.f1:>7.3f}"
            f"{result.false_positive_rate:>8.3f}{result.adjacent_rate:>9.3f}"
            f"{result.app_rate:>9.3f}{result.median_ms:>8.2f}ms{result.p99_ms:>8.2f}ms"
        )
    return lines


def rule_breakdown(result: Result) -> list[str]:
    if not result.rules_on_false_positives:
        return []
    lines = ["", f"False positives by rule - {result.name}"]
    for rule, count in sorted(result.rules_on_false_positives.items(), key=lambda item: -item[1]):
        share = count / max(1, result.false_positives)
        lines.append(f"  {rule:<28}{count:>5}  ({share:.0%} of this detector's false positives)")
    return lines


def adjacent_breakdown(result: Result) -> list[str]:
    if not result.adjacent_by_category:
        return []
    lines = ["", f"Security-adjacent false positives by category - {result.name}"]
    for category, count in sorted(result.adjacent_by_category.items(), key=lambda item: -item[1]):
        lines.append(f"  {category:<28}{count:>5}")
    return lines


def sweep(name: str, scored: list[Scored]) -> list[str]:
    """Precision and recall across the whole range, which is how a threshold gets chosen.

    Derived from scores already computed, because the expensive part is the model and the
    cheap part is the comparison. The value this prints is what belongs on that model's
    registry entry in detect/classifier.py: a threshold is a property of the model rather
    than of this gateway.
    """
    lines = ["", f"Threshold sweep - {name}", ""]
    header = (
        f"{'threshold':>10}{'precision':>11}{'recall':>9}{'F1':>7}"
        f"{'FPR':>8}{'FPR adj':>9}{'FPR app':>9}"
    )
    lines += [header, "-" * len(header)]
    best: tuple[float, float] = (0.0, 0.0)
    for step in range(1, 20):
        cut_off = step / 20
        result = classifier_result(f"{name}@{cut_off}", scored, cut_off)
        lines.append(
            f"{cut_off:>10.2f}{result.precision:>11.3f}{result.recall:>9.3f}{result.f1:>7.3f}"
            f"{result.false_positive_rate:>8.3f}{result.adjacent_rate:>9.3f}{result.app_rate:>9.3f}"
        )
        if result.f1 > best[1]:
            best = (cut_off, result.f1)
    lines.append("")
    lines.append(f"Best F1 at threshold {best[0]:.2f} (F1 {best[1]:.3f})")
    return lines


def load_classifier(name: str) -> Classifier | None:
    directory = KNOWN_MODELS[name].directory(MODEL_DIR)
    if not all((directory / file).is_file() for file in KNOWN_MODELS[name].files):
        print(f"skipping {name}: not fetched (uv run python -m tollgate.detect.fetch {name})")
        return None
    return build_classifier(
        model=name,
        model_dir=MODEL_DIR,
        threshold=None,
        timeout_ms=60_000,  # the eval measures the model, not the gateway's patience with it
        threads=1,
        max_windows=4,
        stride_tokens=64,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="detection_eval")
    parser.add_argument("--model", choices=sorted(KNOWN_MODELS), help="evaluate only this model")
    parser.add_argument("--sweep", action="store_true", help="print a threshold sweep per model")
    parser.add_argument("--no-write", action="store_true", help="print without writing results")
    args = parser.parse_args(argv)

    cases = load_corpus()
    positives = sum(1 for case in cases if case.positive)
    sources = len({case.source for case in cases})
    adjacent = sum(1 for case in cases if case.source == ADJACENT_SOURCE)
    app = sum(1 for case in cases if case.source == APP_SOURCE)

    lines = [
        "Detection evaluation",
        "",
        f"corpus              {len(cases)} cases from {sources} sources",
        f"                    {positives} injections, {len(cases) - positives} benign",
        f"                    of the benign, {adjacent} are hand-written and look hostile,",
        f"                    and {app} are the short prompts an application really sends",
        "",
        "FPR adj is the false positive rate on the adversarial-looking set alone: prompts",
        "about injection, log lines quoting one, ordinary uses of 'ignore the above'.",
        "FPR app is the rate on the ordinary short prompts - 'Summarise this.', 'Fix this",
        "SQL.', 'Hello.' - which is the number that decides whether a detector is usable.",
        "",
    ]

    results: list[Result] = []
    names = [args.model] if args.model else sorted(KNOWN_MODELS)
    sweeps: list[str] = []
    classifiers = [(name, load_classifier(name)) for name in names]

    # The baseline's own row comes from a pass with no model loaded, so its latency is the
    # regex sweep and nothing else.
    results.append(baseline_result(score_corpus(cases, None)))

    for name, classifier in classifiers:
        if classifier is None:
            continue
        print(f"scoring {len(cases)} cases with {name} ...", flush=True)
        scored = score_corpus(cases, classifier)
        threshold = classifier.threshold
        results.append(classifier_result(f"classifier: {name}", scored, threshold))
        results.append(combined_result(f"shipped: baseline+{name}", scored, threshold))
        if args.sweep:
            sweeps += sweep(name, scored)

    lines += table(results)
    for result in results:
        lines += rule_breakdown(result)
    for result in results:
        lines += adjacent_breakdown(result)
    lines += sweeps

    report = "\n".join(lines)
    print(report)
    if not args.no_write:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(report + "\n", encoding="utf-8", newline="\n")
        print(f"\nWrote {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
