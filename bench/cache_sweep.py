"""Phase 6: choosing the semantic cache's threshold, or finding out there isn't one.

A semantic hit is the cache deciding two different requests are the same question. When
it is right it saves a call; when it is wrong the caller gets a confident, well-formed
answer to something nobody asked, and no status code says so. The threshold is the whole
of that decision, so it is measured rather than guessed.

**The method.** A labelled set of prompt pairs (bench/data/prompt_pairs.json), each
marked with whether one may be answered from the other's cached response. Embed both
sides, take the cosine similarity, and sweep a threshold across it. At each threshold:

    precision      of the pairs it called the same, how many were
    recall         of the pairs that were the same, how many it found
    false hits     pairs it called the same that were not - the correctness bugs

The operating point is the lowest threshold with no false hits at all, because precision
and recall are not symmetrical here: a lost hit costs one upstream call, and a false hit
costs the caller a wrong answer they have no way to detect. If no threshold achieves that
with any recall worth having, the honest output is to say so and leave the tier off.

**The negatives are the point.** Half the set is pairs that share almost every word and
mean different things: negations, swapped entities, swapped numbers, swapped directions,
neighbouring topics. A set of obviously unrelated pairs would certify any threshold at
all. These are the ones a cache actually gets wrong.

**What this measures is the embedding model, not the gateway.** Run against the mock, it
characterises the mock's feature hashing - which is a bag of words and bigrams, built to
exercise the code path rather than to understand language. Run against the real provider,
it characterises the real embeddings. The committed result says which one produced it,
because a threshold carried over from one to the other would be exactly the unfounded
transfer this file exists to prevent.

    docker compose up -d --wait
    uv run python bench/cache_sweep.py

    # against the real provider, which costs a fraction of a cent
    uv run python bench/cache_sweep.py \\
        --upstream https://generativelanguage.googleapis.com --api-key "$GEMINI_API_KEY"
"""

import argparse
import asyncio
import json
import pathlib
from collections import Counter
from dataclasses import dataclass

import httpx

from tollgate.cache.semantic import Embedder, cosine_similarity
from tollgate.config import get_settings

DATA = pathlib.Path("bench/data/prompt_pairs.json")

# Cosine similarity below about 0.3 is noise for any embedding model worth using, and a
# threshold that low would call everything a hit. The sweep starts where a decision could
# plausibly be made and runs to 1.0, where only identical vectors match.
SWEEP_START = 0.30
SWEEP_STEP = 0.01

# How far below the zero-false-hit threshold the recommendation sits. The labelled set is
# a sample: the closest false pair in it landed at some score, and the next one the
# gateway meets could land a little higher. The margin is the room that costs recall in
# exchange for not discovering the difference in production.
SAFETY_MARGIN = 0.02


@dataclass(frozen=True)
class Pair:
    kind: str
    same: bool
    a: str
    b: str
    similarity: float = 0.0


@dataclass(frozen=True)
class Point:
    """One threshold, and what it would have decided."""

    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def predicted_same(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def precision(self) -> float:
        return self.true_positives / self.predicted_same if self.predicted_same else 1.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 0.0

    @property
    def false_hit_rate(self) -> float:
        """Of every pair offered, how many would have been answered wrongly.

        The denominator is every pair and not only the negatives, because this is the
        number a person running the gateway cares about: out of the traffic, how often
        does the cache lie.
        """
        total = (
            self.true_positives + self.false_positives + self.false_negatives + self.true_negatives
        )
        return self.false_positives / total if total else 0.0

    @property
    def f1(self) -> float:
        if not self.precision or not self.recall:
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)


def load_pairs(path: pathlib.Path) -> list[Pair]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        Pair(kind=item["kind"], same=bool(item["same"]), a=item["a"], b=item["b"])
        for item in payload["pairs"]
    ]


async def score(pairs: list[Pair], embedder: Embedder) -> list[Pair]:
    """Every pair, with its cosine similarity. One embedding per distinct prompt."""
    prompts = sorted({prompt for pair in pairs for prompt in (pair.a, pair.b)})
    vectors: dict[str, list[float]] = {}
    for index, prompt in enumerate(prompts, start=1):
        embedding = await embedder.embed(prompt)
        if embedding is None:
            raise SystemExit(f"Could not embed prompt {index} of {len(prompts)}: {prompt!r}")
        vectors[prompt] = embedding.values
        if index % 20 == 0:
            print(f"  embedded {index}/{len(prompts)}")
    return [
        Pair(
            kind=pair.kind,
            same=pair.same,
            a=pair.a,
            b=pair.b,
            similarity=cosine_similarity(vectors[pair.a], vectors[pair.b]),
        )
        for pair in pairs
    ]


def sweep(pairs: list[Pair]) -> list[Point]:
    points = []
    threshold = SWEEP_START
    while threshold <= 1.0 + 1e-9:
        predicted = [(pair.same, pair.similarity >= threshold) for pair in pairs]
        points.append(
            Point(
                threshold=round(threshold, 4),
                true_positives=sum(1 for same, hit in predicted if same and hit),
                false_positives=sum(1 for same, hit in predicted if not same and hit),
                false_negatives=sum(1 for same, hit in predicted if same and not hit),
                true_negatives=sum(1 for same, hit in predicted if not same and not hit),
            )
        )
        threshold += SWEEP_STEP
    return points


def operating_point(points: list[Point]) -> Point | None:
    """The lowest threshold that makes no false hits at all, or None if none does.

    Lowest, because among the thresholds that are safe the useful one is the one that
    still finds the most true hits. None is a real answer: it means this embedding model
    cannot separate the two classes on this set, and the tier should stay off.
    """
    clean = [point for point in points if point.false_positives == 0 and point.recall > 0]
    return min(clean, key=lambda point: point.threshold) if clean else None


def recommended(points: list[Point]) -> Point | None:
    """The operating point with the safety margin applied, as a threshold to actually use."""
    safe = operating_point(points)
    if safe is None:
        return None
    wanted = safe.threshold + SAFETY_MARGIN
    at_or_above = [point for point in points if point.threshold >= wanted - 1e-9]
    return min(at_or_above, key=lambda point: point.threshold) if at_or_above else safe


def report(pairs: list[Pair], points: list[Point], source: str) -> str:
    positives = sum(1 for pair in pairs if pair.same)
    lines = [
        f"{len(pairs)} labelled prompt pairs: {positives} that may share an answer and",
        f"{len(pairs) - positives} that may not. Embeddings from {source}.",
        "",
        "A false hit is a pair the threshold called the same when it was not - a caller",
        "receiving a confident answer to a question nobody asked. A missed hit is one",
        "upstream call. They are not the same kind of mistake, which is why the operating",
        "point is chosen by precision and recall is whatever it turns out to be.",
        "",
        f"{'thresh':>7} {'precision':>10} {'recall':>8} {'F1':>7} {'false hits':>11} "
        f"{'false hit rate':>15}",
    ]
    for point in points:
        if round(point.threshold * 100) % 5 and point.false_positives:
            continue  # every 0.05, plus every threshold that still makes a false hit
        lines.append(
            f"{point.threshold:>7.2f} {point.precision:>10.3f} {point.recall:>8.3f} "
            f"{point.f1:>7.3f} {point.false_positives:>11} {point.false_hit_rate:>14.1%}"
        )

    safe = operating_point(points)
    choice = recommended(points)
    lines.append("")
    if safe is None or choice is None:
        worst = max((pair for pair in pairs if not pair.same), key=lambda pair: pair.similarity)
        best_positive = min((pair for pair in pairs if pair.same), key=lambda pair: pair.similarity)
        lines += [
            "NO USABLE THRESHOLD.",
            "",
            "There is no cut-off that finds a single true pair without also making a false",
            "hit, because the classes overlap: the highest-scoring pair that must not match",
            f"scores {worst.similarity:.4f} ({worst.kind}), while the lowest-scoring pair that",
            f"must match scores {best_positive.similarity:.4f} ({best_positive.kind}). Every",
            "threshold between them is wrong in one direction or the other, and no threshold",
            "outside them is useful.",
            "",
            f"    must not match, highest:  {worst.similarity:.4f}  {worst.a!r}",
            f"                              {'':>6}  {worst.b!r}",
            f"    must match, lowest:       {best_positive.similarity:.4f}  {best_positive.a!r}",
            f"                              {'':>6}  {best_positive.b!r}",
            "",
            "The conclusion is about the embedding model, not about the cache: these",
            "embeddings do not carry the distinctions the negatives turn on. The semantic",
            "tier therefore stays off (CACHE_SEMANTIC_ENABLED=False) until a sweep against",
            "embeddings that can separate them produces a threshold.",
        ]
    else:
        lines += [
            f"Lowest threshold with no false hits:  {safe.threshold:.2f} "
            f"(recall {safe.recall:.1%})",
            f"Recommended, with a {SAFETY_MARGIN:.2f} margin:      {choice.threshold:.2f} "
            f"(recall {choice.recall:.1%}, false hit rate {choice.false_hit_rate:.1%})",
            "",
            "    CACHE_SEMANTIC_ENABLED=True",
            f"    CACHE_SEMANTIC_THRESHOLD={choice.threshold:.2f}",
        ]

    lines += ["", "By kind, mean similarity:", ""]
    kinds: dict[str, list[float]] = {}
    labels: dict[str, bool] = {}
    for pair in pairs:
        kinds.setdefault(pair.kind, []).append(pair.similarity)
        labels[pair.kind] = pair.same
    for kind in sorted(kinds, key=lambda k: (not labels[k], -sum(kinds[k]) / len(kinds[k]))):
        scores = kinds[kind]
        mark = "may share" if labels[kind] else "MUST NOT "
        lines.append(
            f"  {mark}  {kind:<14} n={len(scores):<3} "
            f"mean {sum(scores) / len(scores):.4f}  "
            f"min {min(scores):.4f}  max {max(scores):.4f}"
        )
    return "\n".join(lines)


# --- the figure -----------------------------------------------------------------------

WIDTH, HEIGHT = 720, 420
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 62, 150, 30, 52


def svg(points: list[Point], source: str) -> str:
    """Precision and recall against threshold, as a standalone SVG.

    Hand-written rather than plotted with a library: one figure does not justify a
    dependency, and the arithmetic is two loops.
    """
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    low = points[0].threshold
    high = points[-1].threshold

    def x_of(threshold: float) -> float:
        return PAD_LEFT + (threshold - low) / (high - low) * plot_w

    def y_of(value: float) -> float:
        return PAD_TOP + (1 - value) * plot_h

    def path(values: list[tuple[float, float]]) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x_of(t):.1f},{y_of(v):.1f}"
            for i, (t, v) in enumerate(values)
        )

    precision = path([(p.threshold, p.precision) for p in points])
    recall = path([(p.threshold, p.recall) for p in points])
    false_rate = path([(p.threshold, p.false_hit_rate) for p in points])

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" font-family="ui-monospace,Menlo,Consolas,monospace">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
        f'<text x="{PAD_LEFT}" y="18" font-size="13" fill="#171b21">'
        f"Semantic cache threshold sweep &#183; {source}</text>",
    ]
    for step in range(6):
        value = step / 5
        y = y_of(value)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.1f}" x2="{PAD_LEFT + plot_w}" y2="{y:.1f}" '
            f'stroke="#e3e6eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 8}" y="{y + 4:.1f}" font-size="11" fill="#6c7684" '
            f'text-anchor="end">{value:.1f}</text>'
        )
    tick = low
    while tick <= high + 1e-9:
        if round(tick * 100) % 10 == 0:
            x = x_of(tick)
            parts.append(
                f'<line x1="{x:.1f}" y1="{PAD_TOP + plot_h}" x2="{x:.1f}" '
                f'y2="{PAD_TOP + plot_h + 5}" stroke="#6c7684" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{x:.1f}" y="{PAD_TOP + plot_h + 20:.1f}" font-size="11" '
                f'fill="#6c7684" text-anchor="middle">{tick:.1f}</text>'
            )
        tick += SWEEP_STEP
    parts.append(
        f'<text x="{PAD_LEFT + plot_w / 2:.1f}" y="{HEIGHT - 12}" font-size="12" '
        f'fill="#3d4c5e" text-anchor="middle">cosine similarity threshold</text>'
    )

    for d, colour, label, y in (
        (precision, "#2c6e52", "precision", PAD_TOP + 14),
        (recall, "#3d5166", "recall", PAD_TOP + 34),
        (false_rate, "#9e3128", "false hit rate", PAD_TOP + 54),
    ):
        parts.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2"/>')
        parts.append(
            f'<line x1="{PAD_LEFT + plot_w + 14}" y1="{y - 4}" '
            f'x2="{PAD_LEFT + plot_w + 34}" y2="{y - 4}" stroke="{colour}" stroke-width="2"/>'
        )
        parts.append(
            f'<text x="{PAD_LEFT + plot_w + 40}" y="{y}" font-size="11" fill="#171b21">'
            f"{label}</text>"
        )

    choice = recommended(points)
    if choice is not None:
        x = x_of(choice.threshold)
        parts.append(
            f'<line x1="{x:.1f}" y1="{PAD_TOP}" x2="{x:.1f}" y2="{PAD_TOP + plot_h}" '
            f'stroke="#a2501e" stroke-width="1.5" stroke-dasharray="4 3"/>'
        )
        parts.append(
            f'<text x="{x + 6:.1f}" y="{PAD_TOP + 12}" font-size="11" fill="#a2501e">'
            f"chosen {choice.threshold:.2f}</text>"
        )
    else:
        parts.append(
            f'<text x="{PAD_LEFT + plot_w / 2:.1f}" y="{PAD_TOP + plot_h / 2:.1f}" '
            f'font-size="14" fill="#9e3128" text-anchor="middle">'
            f"no threshold without false hits</text>"
        )

    parts.append(
        f'<rect x="{PAD_LEFT}" y="{PAD_TOP}" width="{plot_w}" height="{plot_h}" '
        f'fill="none" stroke="#d5d9e0" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


async def collect(args: argparse.Namespace) -> tuple[list[Pair], str]:
    settings = get_settings()
    base_url = args.upstream or settings.upstream_base_url
    api_key = args.api_key or settings.gemini_api_key.get_secret_value()
    pairs = load_pairs(pathlib.Path(args.data))
    print(f"{len(pairs)} pairs, embedding against {base_url}")

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        embedder = Embedder(
            client,
            api_key,
            model=args.model or settings.cache_embedding_model,
            dimensions=args.dimensions or settings.cache_embedding_dimensions,
            timeout_s=30.0,
        )
        scored = await score(pairs, embedder)
    source = f"{embedder.model} at {base_url}"
    return scored, source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--upstream", default=None, help="defaults to UPSTREAM_BASE_URL")
    parser.add_argument("--api-key", default=None, help="defaults to GEMINI_API_KEY")
    parser.add_argument("--model", default=None)
    parser.add_argument("--dimensions", type=int, default=None)
    parser.add_argument("--out", default="bench/results/cache_sweep.txt")
    parser.add_argument("--figure", default="bench/results/cache_sweep.svg")
    args = parser.parse_args()

    pairs, source = asyncio.run(collect(args))
    points = sweep(pairs)
    text = report(pairs, points, source)
    print("\n" + text)

    kinds = Counter(pair.kind for pair in pairs)
    print(f"\n{len(kinds)} kinds of pair")

    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"Written to {out}")
    if args.figure:
        figure = pathlib.Path(args.figure)
        figure.parent.mkdir(parents=True, exist_ok=True)
        figure.write_text(svg(points, source) + "\n", encoding="utf-8")
        print(f"Figure written to {figure}")


if __name__ == "__main__":
    main()
