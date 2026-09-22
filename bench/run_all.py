"""Regenerate every number in the README, in one command.

    docker compose up -d --wait
    OTEL_ENABLED=True METRICS_ENDPOINT_ENABLED=True \
        uv run uvicorn tollgate.main:app --port 8000    # in another terminal
    uv run python bench/run_all.py tg_your_tenant_key

Each measurement is a separate script, because each one wants its own arguments and its
own explanation, and a reader chasing one number should land in a file about that number
rather than in this one. What this adds is the order, the arguments the committed results
were produced with, and - the part that matters - a refusal to produce a number under
conditions that would make it wrong.

**Some numbers need a gateway configured differently from the one you have running**, and
this cannot reconfigure a process it did not start. The semantic cache tier ships off; the
classifier tier ships unset; the limiter ships in memory. A run against a gateway missing
one of those cannot produce that row, and the choice is between skipping it and printing a
number measured with the feature switched off under a heading that says it was on. It
skips, says which setting was missing, and prints the command that would fix it. A results
directory with three stale files and a report that says so is worth more than one with
eleven fresh files where three are quietly lying.

Configuration is read from this process's environment, which is the same `.env` the
gateway reads. If you started the gateway with different settings on its command line,
that is invisible from here, and the skip decisions below will be wrong in whichever
direction you changed. Nothing can see inside another process; this is the assumption, and
it is written down rather than hidden.

The load test runs last and takes several minutes, because its steady sweep is six arrival
rates at forty-five seconds each.
"""

import argparse
import dataclasses
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable

import httpx

HERE = pathlib.Path(__file__).resolve().parent
RESULTS = HERE / "results"
ROOT = HERE.parent


@dataclasses.dataclass(frozen=True)
class Step:
    """One measurement: what to run, where its output belongs, and when not to run it."""

    name: str
    summary: str
    command: list[str]
    # Where to put stdout, for the scripts that only print. None means the script writes
    # its own file and this should not second-guess where.
    capture: str | None = None
    # Returns a reason to skip, or None to go ahead.
    guard: Callable[["Context"], str | None] | None = None
    minutes: float = 1.0


@dataclasses.dataclass
class Context:
    gateway: str
    tenant_key: str
    env: dict[str, str]

    def setting(self, name: str, default: str = "") -> str:
        return self.env.get(name, default)

    def truthy(self, name: str) -> bool:
        return self.setting(name).strip().lower() in {"1", "true", "yes", "on"}

    def metrics(self) -> str:
        try:
            response = httpx.get(f"{self.gateway}/metrics", timeout=10.0)
        except httpx.HTTPError:
            return ""
        return response.text if response.status_code == 200 else ""


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def needs_gateway(context: Context) -> str | None:
    try:
        response = httpx.get(f"{context.gateway}/readyz", timeout=10.0)
    except httpx.HTTPError as exc:
        return f"the gateway is not answering on {context.gateway} ({exc})"
    if response.status_code != 200:
        return f"the gateway is not ready ({response.status_code})"
    return None


def needs_metrics(context: Context) -> str | None:
    if (reason := needs_gateway(context)) is not None:
        return reason
    if "tollgate_overhead_milliseconds" not in context.metrics():
        return (
            "the gateway records no metrics, so overhead cannot be measured - restart it "
            "with OTEL_ENABLED=True METRICS_ENDPOINT_ENABLED=True"
        )
    return None


def needs_semantic_cache(context: Context) -> str | None:
    if (reason := needs_gateway(context)) is not None:
        return reason
    if not context.truthy("CACHE_SEMANTIC_ENABLED"):
        return (
            "the semantic tier is off, which is how it ships - set CACHE_SEMANTIC_ENABLED "
            "and a threshold and restart the gateway to measure it"
        )
    return None


def needs_classifier(context: Context) -> str | None:
    model = context.setting("DETECTION_CLASSIFIER_MODEL").strip()
    if not model:
        return (
            "no classifier is configured, so only the regex baseline would be scored - set "
            "DETECTION_CLASSIFIER_MODEL after `python -m tollgate.detect.fetch <model>`"
        )
    directory = ROOT / context.setting("DETECTION_MODEL_DIR", "models") / model
    if not directory.exists():
        return f"DETECTION_CLASSIFIER_MODEL={model} is set but {directory} is not there"
    return None


def needs_redis(context: Context) -> str | None:
    url = context.setting("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis  # imported here so the rest of the run works without it
    except ImportError:
        return "the redis package is not installed"
    try:
        redis.Redis.from_url(url).ping()
    # Any failure to reach it is the same answer, and redis raises several unrelated types.
    except Exception as exc:
        return f"Redis is not answering on {url} ({exc})"
    return None


# ---------------------------------------------------------------------------
# The measurements, in the order the README reads them
# ---------------------------------------------------------------------------


def steps(context: Context) -> list[Step]:
    key, gateway = context.tenant_key, context.gateway
    return [
        Step(
            name="baseline_latency",
            summary="gateway overhead against calling the provider directly, sequential",
            command=["bench/baseline_latency.py", key, "--gateway", gateway, "--requests", "300"],
            capture="baseline_latency.txt",
            guard=needs_gateway,
            minutes=2,
        ),
        Step(
            name="stream_ttft",
            summary="added time to first token on a streamed response",
            command=["bench/stream_ttft.py", key, "--gateway", gateway, "--requests", "60"],
            capture="stream_ttft.txt",
            guard=needs_gateway,
            minutes=2,
        ),
        Step(
            name="limit_accuracy",
            summary="rate limit error in process against Redis, across container counts",
            command=["bench/limit_accuracy.py"],
            capture="limit_accuracy.txt",
            guard=needs_redis,
            minutes=1,
        ),
        Step(
            name="reservation_error",
            summary="streamed budget reservation against what the request really cost",
            command=["bench/reservation_error.py", key, "--gateway", gateway],
            capture="reservation_error.txt",
            guard=needs_redis,
            minutes=1,
        ),
        Step(
            name="rollup_plan",
            summary="the rollup query before and after its index, with both plans",
            command=["bench/rollup_plan.py"],
            minutes=3,
        ),
        Step(
            name="cache_savings",
            summary="exact cache hit rate, latency and spend avoided, over five traffic shapes",
            command=["bench/cache_savings.py", key, "--gateway", gateway],
            guard=needs_gateway,
            minutes=6,
        ),
        Step(
            name="cache_savings_semantic",
            summary="the same traffic with the semantic tier on, a quarter of it reworded",
            command=[
                "bench/cache_savings.py",
                key,
                "--gateway",
                gateway,
                "--variants",
                "0.25",
                "--out",
                "bench/results/cache_savings_semantic.txt",
            ],
            guard=needs_semantic_cache,
            minutes=6,
        ),
        Step(
            name="cache_sweep",
            summary="the semantic threshold precision-recall curve, against the mock's embeddings",
            command=["bench/cache_sweep.py"],
            minutes=1,
        ),
        Step(
            name="detection_eval",
            summary="injection precision and recall, every tier, over the committed corpus",
            command=["bench/detection_eval.py", "--sweep"],
            guard=needs_classifier,
            minutes=8,
        ),
        Step(
            name="load_test",
            summary="steady sweep, burst, degraded upstream, and cache warm against cold",
            command=["bench/load_test.py", key, "--gateway", gateway, "--quiet"],
            guard=needs_metrics,
            minutes=12,
        ),
    ]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Outcome:
    step: Step
    status: str  # "ran", "failed", "skipped"
    seconds: float = 0.0
    detail: str = ""


def run_step(step: Step, dry_run: bool) -> Outcome:
    command = [sys.executable, *step.command]
    if dry_run:
        return Outcome(step, "skipped", detail="dry run")

    started = time.perf_counter()
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    seconds = time.perf_counter() - started

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-4:]
        return Outcome(step, "failed", seconds, " / ".join(tail) or f"exit {completed.returncode}")

    if step.capture:
        destination = RESULTS / step.capture
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(completed.stdout, encoding="utf-8", newline="\n")
    return Outcome(step, "ran", seconds)


def summarise(outcomes: list[Outcome]) -> str:
    lines = [
        "",
        "SUMMARY",
        "",
        f"{'measurement':<24} {'status':<8} {'time':>8}  note",
    ]
    for outcome in outcomes:
        minutes = f"{outcome.seconds / 60:.1f}m" if outcome.seconds else "-"
        lines.append(f"{outcome.step.name:<24} {outcome.status:<8} {minutes:>8}  {outcome.detail}")
    skipped = [o for o in outcomes if o.status == "skipped"]
    failed = [o for o in outcomes if o.status == "failed"]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} measurement(s) failed. Those results are now stale.")
    if skipped:
        lines.append(
            f"{len(skipped)} measurement(s) were skipped, and the files under bench/results/ "
            "for those\nare whatever the last run that could produce them left there. They are "
            "not wrong,\nbut they are not from this run either."
        )
    if not failed and not skipped:
        lines.append("Every number in the README came from this run.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tenant_key")
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--only", nargs="+", default=None, help="run just these measurements")
    parser.add_argument("--skip", nargs="+", default=[], help="skip these measurements")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan and the guards, run nothing"
    )
    args = parser.parse_args()

    context = Context(gateway=args.gateway, tenant_key=args.tenant_key, env=dict(os.environ))
    planned = steps(context)
    if args.only:
        planned = [s for s in planned if s.name in set(args.only)]
    planned = [s for s in planned if s.name not in set(args.skip)]
    if not planned:
        raise SystemExit("Nothing selected to run.")

    total = sum(s.minutes for s in planned)
    print(f"{len(planned)} measurements, roughly {total:.0f} minutes if none are skipped.\n")

    outcomes: list[Outcome] = []
    for step in planned:
        reason = step.guard(context) if step.guard else None
        if reason is not None:
            print(f"- {step.name}: skipped, {reason}", flush=True)
            outcomes.append(Outcome(step, "skipped", detail=reason))
            continue
        print(f"- {step.name}: {step.summary} (~{step.minutes:.0f}m)", flush=True)
        outcome = run_step(step, args.dry_run)
        if outcome.status == "failed":
            print(f"  failed: {outcome.detail}", flush=True)
        outcomes.append(outcome)

    print(summarise(outcomes))
    if any(o.status == "failed" for o in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
