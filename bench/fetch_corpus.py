"""Build the evaluation corpus from published datasets, once, into bench/data/.

    uv run python bench/fetch_corpus.py

Writes `bench/data/detection_corpus.jsonl`, which is committed. The corpus is a build
artefact of this script and is kept in the repository on purpose: an eval whose data is
fetched at run time is an eval whose numbers cannot be reproduced once the source moves, and
"published precision" means nothing if the test set is whatever the internet served that day.
Both sources are Apache-2.0, so redistributing a subset is allowed; `bench/data/SOURCES.md`
records where each row came from.

**What the corpus is made of.**

  * Positives from `deepset/prompt-injections` (injections and jailbreaks, multilingual) and
    `jackhhao/jailbreak-classification` (long roleplay jailbreaks - DAN and its relatives).
  * Negatives from the same two datasets' benign halves, which are ordinary questions and
    harmless roleplay prompts.
  * Negatives from `bench/data/security_adjacent.jsonl`, which is written by hand and is the
    interesting part: benign prompts that look hostile.
  * Negatives from `bench/data/app_prompts.jsonl`, also written by hand, and the set that
    separates the detectors most sharply. They are not adversarial at all - "Summarise this.",
    "Fix this SQL.", "Hello." - they are simply what an application actually sends. The public
    benchmarks' benign halves are full sentences and roleplay prompts, so a classifier can
    score well on them and still flag half of a real product's traffic.

**Sampling is deterministic.** A fixed seed and a per-source cap, so re-running this produces
the same corpus and a number measured last week can be compared with one measured today. The
caps keep the repository small and the eval quick enough to sit in CI; they are applied after
a shuffle, so a cap does not mean "the first N rows of whatever order the source used".
"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
DATA = HERE / "data"
CORPUS = DATA / "detection_corpus.jsonl"
ADJACENT = DATA / "security_adjacent.jsonl"
APP = DATA / "app_prompts.jsonl"

API = "https://datasets-server.huggingface.co/rows"
PAGE = 100
SEED = 20260921

# Labels, as the corpus records them. `injection` is the positive class.
INJECTION = "injection"
BENIGN = "benign"


@dataclass(frozen=True)
class Source:
    """One published dataset, and how to read its labels."""

    dataset: str
    config: str
    split: str
    text_field: str
    label_field: str
    # Which values of `label_field` mean an injection attempt. Everything else is benign.
    positive_labels: frozenset[str]
    # How many rows to keep, per class, after shuffling.
    cap: int


SOURCES = (
    # 662 rows, roughly half injections, in several languages. Small and noisy, which is the
    # state of this whole benchmark landscape and worth saying out loud in the README.
    Source(
        dataset="deepset/prompt-injections",
        config="default",
        split="train",
        text_field="text",
        label_field="label",
        positive_labels=frozenset({"1"}),
        cap=300,
    ),
    # Long roleplay jailbreaks against ordinary roleplay prompts. The benign half of this one
    # matters more than the positives: "you are a devoted fan of a celebrity" is exactly the
    # shape a persona rule fires on by mistake.
    Source(
        dataset="jackhhao/jailbreak-classification",
        config="default",
        split="train",
        text_field="prompt",
        label_field="type",
        positive_labels=frozenset({"jailbreak"}),
        cap=300,
    ),
)


def read_page(url: str, attempts: int = 5) -> dict[str, Any]:
    """One page, with retries. The datasets server answers 502 under load often enough that
    a script without this fails about one run in three, and a corpus that is a nuisance to
    rebuild is a corpus nobody rebuilds."""
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "tollgate"})
            with urllib.request.urlopen(request, timeout=60) as response:
                page: dict[str, Any] = json.load(response)
                return page
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise
            delay = 2**attempt
            print(f"  {type(exc).__name__}; retrying in {delay}s")
            time.sleep(delay)
    raise AssertionError("unreachable")


def fetch(source: Source) -> list[dict[str, Any]]:
    """Every row of one split, through the datasets server's JSON API.

    The JSON API rather than the parquet files, so this script needs neither pyarrow nor
    pandas - dependencies that would exist in this project for one script that runs twice a
    year.
    """
    rows: list[dict[str, Any]] = []
    while True:
        query = urllib.parse.urlencode(
            {
                "dataset": source.dataset,
                "config": source.config,
                "split": source.split,
                "offset": len(rows),
                "length": PAGE,
            }
        )
        page = read_page(f"{API}?{query}")
        batch = [item["row"] for item in page["rows"]]
        rows.extend(batch)
        print(f"  {source.dataset}: {len(rows)}/{page['num_rows_total']}")
        time.sleep(0.2)  # the server is free and shared; do not hammer it
        if len(rows) >= page["num_rows_total"] or not batch:
            return rows


def normalise(source: Source, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One shape for every row, whatever the source called its columns."""
    cases = []
    for row in rows:
        text = str(row.get(source.text_field) or "").strip()
        if not text:
            continue
        label = str(row.get(source.label_field))
        cases.append(
            {
                "text": text,
                "label": INJECTION if label in source.positive_labels else BENIGN,
                "source": source.dataset,
            }
        )
    return cases


def sample(cases: list[dict[str, str]], cap: int, rng: random.Random) -> list[dict[str, str]]:
    """Up to `cap` of each class, shuffled first so a cap is a sample and not a prefix."""
    kept: list[dict[str, str]] = []
    for label in (INJECTION, BENIGN):
        matching = [case for case in cases if case["label"] == label]
        rng.shuffle(matching)
        kept.extend(matching[:cap])
    return kept


def hand_written(path: Path, source: str) -> list[dict[str, str]]:
    """One of the hand-written sets. Committed, not generated; see each file's header."""
    if not path.is_file():
        raise SystemExit(f"{path} is missing; it is committed, not generated")
    # split("\n"), not splitlines(): see load_corpus in detection_eval.py.
    lines = path.read_text(encoding="utf-8").split("\n")
    rows = [json.loads(line) for line in lines if line.strip()]
    return [{**row, "source": source} for row in rows]


def main() -> None:
    rng = random.Random(SEED)
    corpus: list[dict[str, str]] = []
    for source in SOURCES:
        print(f"{source.dataset} ...")
        corpus.extend(sample(normalise(source, fetch(source)), source.cap, rng))
    corpus.extend(hand_written(ADJACENT, "tollgate/security-adjacent"))
    corpus.extend(hand_written(APP, "tollgate/app-prompts"))

    # Sorted before writing, so the committed file has a stable order and a re-fetch produces
    # a diff only where the data really changed.
    corpus.sort(key=lambda case: (case["source"], case["label"], case["text"]))
    DATA.mkdir(parents=True, exist_ok=True)
    with CORPUS.open("w", encoding="utf-8", newline="\n") as handle:
        for case in corpus:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    counts: dict[tuple[str, str], int] = {}
    for case in corpus:
        key = (case["source"], case["label"])
        counts[key] = counts.get(key, 0) + 1
    print(f"\nWrote {len(corpus)} cases to {CORPUS}")
    for (source_name, label), count in sorted(counts.items()):
        print(f"  {source_name:<38} {label:<10} {count:>5}")


if __name__ == "__main__":
    main()
