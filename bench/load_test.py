"""Phase 8: what the gateway costs under load, where it stops, and how it fails.

Four scenarios, each answering one question, all of them against the mock so the number
is the gateway's and not the provider's mood on the day:

  * **steady** - the gateway's own cost at a rate it comfortably serves, swept upwards
    until it stops serving it. The sweep is what produces the throughput ceiling.
  * **burst** - quiet, a sudden multiple, quiet again. The number is the third phase
    against the first: a gateway that queues a spike is still slow after it ends.
  * **degraded** - the same steady load against an upstream failing a fifth of the time
    and slow for a tenth of the rest.
  * **cache** - identical work twice, cold then warm.

**Where the overhead number comes from.** Not from k6. `http_req_duration` is dominated
by the mock holding each request for 100 ms, and subtracting one distribution from
another is wrong exactly at the tail, which is the end that matters. The gateway has
recorded total-minus-upstream per request since phase 5, so this scrapes `/metrics`
either side of each run, diffs the histogram buckets, and interpolates from the delta.
That is the same series the dashboard's overhead panel reads and the same one the alert
watches, which means this benchmark and the alert cannot disagree about what overhead is.

Interpolating inside a bucket is an estimate, and its error is the bucket's width. A
percentile that landed in a bucket narrow enough for a single figure to mean something
is printed as one; anything else is printed as the bucket. The mean beside it is exact -
the histogram's own sum over its own count - and it is the column to compare between
rows.

**k6 runs in Docker**, so nothing has to be installed and the run is the same on any
machine. The scripts are mounted read-only and write their summaries to a temporary
directory this script reads back.

    docker compose up -d --wait
    OTEL_ENABLED=True METRICS_ENDPOINT_ENABLED=True \
        uv run uvicorn tollgate.main:app --port 8000    # in another terminal
    uv run python bench/load_test.py tg_your_tenant_key

Both telemetry settings are needed, and they are not the same switch: the endpoint one
serves `/metrics`, and `OTEL_ENABLED` is what installs the meter provider that puts the
gateway's own series on it. With only the first, `/metrics` answers 200 with the
process's default Python metrics and none of the gateway's, and every overhead column
comes back empty after a run that otherwise looked perfect. Preflight checks for the
histogram rather than for the endpoint, for that reason.

The tenant must have no rate limit: a limiter capping the offered load would make the
ceiling a measurement of the limit rather than of the gateway, and this refuses to run
rather than report one. `tollgate set-limits <tenant> --unlimited`.
"""

import argparse
import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tollgate.auth import hash_key
from tollgate.config import get_settings
from tollgate.db.models import ApiKey, CacheEntry, Tenant

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE / "load"
K6_IMAGE = "grafana/k6:latest"

OVERHEAD_METRIC = "tollgate_overhead_milliseconds"
UPSTREAM_METRIC = "tollgate_upstream_duration_milliseconds"
REQUEST_METRIC = "tollgate_request_duration_milliseconds"

Sessionmaker = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------------------
# Prometheus scraping
# ---------------------------------------------------------------------------

SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)$"
)
LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')

# How narrow a bucket has to be, relative to the value in it, before a single figure is
# an honest way to print that value. A percentile a quarter of the way into a bucket
# twice as wide as the number itself is a range, and printing it as a point is a lie of
# three significant figures.
POINT_ESTIMATE_RATIO = 0.25


@dataclass(frozen=True)
class Estimate:
    """A percentile read off a bucketed histogram, with the bucket it was read from."""

    value: float
    lower: float
    upper: float

    def __str__(self) -> str:
        if self.upper == float("inf"):
            return f">{self.lower:g}ms"
        width = self.upper - self.lower
        if width <= max(1.0, POINT_ESTIMATE_RATIO * self.value):
            return f"{self.value:.1f}ms"
        return f"{self.lower:g}-{self.upper:g}ms"


@dataclass
class Histogram:
    """Cumulative bucket counts, keyed by upper bound, summed over one tenant's series.

    The gateway labels by tenant, model, method and outcome, and this benchmark offers
    one tenant one model, so the label sets are summed back together. Only that tenant's
    though: anything else pointed at the same gateway - a demo generator left running in
    another window - would otherwise be counted as part of the run, and the measurement
    would depend on nothing else happening rather than on nothing else being counted.
    """

    buckets: dict[float, float] = field(default_factory=dict)
    count: float = 0.0
    total: float = 0.0

    def minus(self, earlier: "Histogram") -> "Histogram":
        bounds = set(self.buckets) | set(earlier.buckets)
        return Histogram(
            buckets={b: self.buckets.get(b, 0.0) - earlier.buckets.get(b, 0.0) for b in bounds},
            count=self.count - earlier.count,
            total=self.total - earlier.total,
        )

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None

    def percentile(self, p: float) -> "Estimate | None":
        """Where the pth percentile fell, and which bucket it fell in.

        The bucket is carried along rather than discarded. A percentile that landed
        between the 100 ms and 250 ms boundaries is known to within 150 ms, and printing
        it as `175.0ms` claims three significant figures the histogram never had - worse,
        two different metrics landing in that bucket print as the same number and look
        like a suspicious coincidence rather than like the coarse reading they are.
        """
        if self.count <= 0:
            return None
        target = self.count * p / 100
        bounds = sorted(b for b in self.buckets if b != float("inf"))
        lower = 0.0
        previous = 0.0
        for bound in bounds:
            cumulative = self.buckets[bound]
            if cumulative >= target:
                span = cumulative - previous
                within = (target - previous) / span if span > 0 else 0.0
                return Estimate(lower + (bound - lower) * within, lower, bound)
            lower, previous = bound, cumulative
        # Past the last finite bound, so all that is known is "above it".
        return Estimate(bounds[-1], bounds[-1], float("inf")) if bounds else None


def parse_histogram(text: str, metric: str, tenant: str) -> Histogram:
    histogram = Histogram()
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(metric):
            continue
        match = SAMPLE.match(line.strip())
        if match is None:
            continue
        name, labels, raw = match["name"], match["labels"] or "", match["value"]
        parsed = dict(LABEL.findall(labels))
        if parsed.get("tenant") != tenant:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if name == f"{metric}_bucket":
            bound = parsed.get("le")
            if bound is None:
                continue
            key = float("inf") if bound in {"+Inf", "Inf"} else float(bound)
            histogram.buckets[key] = histogram.buckets.get(key, 0.0) + value
        elif name == f"{metric}_count":
            histogram.count += value
        elif name == f"{metric}_sum":
            histogram.total += value
    return histogram


def scrape(gateway: str) -> str:
    response = httpx.get(f"{gateway}/metrics", timeout=15.0)
    if response.status_code == 404:
        raise SystemExit(
            "The gateway is not serving /metrics. Overhead is read from its own histogram,\n"
            "so start it with METRICS_ENDPOINT_ENABLED=True and run this again."
        )
    response.raise_for_status()
    return response.text


@dataclass
class Overhead:
    """What the gateway's own histograms said about one run, for one tenant."""

    overhead: Histogram
    upstream: Histogram
    total: Histogram

    @classmethod
    def between(cls, before: str, after: str, tenant: str) -> "Overhead":
        def delta(metric: str) -> Histogram:
            return parse_histogram(after, metric, tenant).minus(
                parse_histogram(before, metric, tenant)
            )

        return cls(
            overhead=delta(OVERHEAD_METRIC),
            upstream=delta(UPSTREAM_METRIC),
            total=delta(REQUEST_METRIC),
        )


# ---------------------------------------------------------------------------
# k6
# ---------------------------------------------------------------------------


@dataclass
class K6Run:
    name: str
    metrics: dict[str, Any]
    meta: dict[str, Any]
    seconds: float
    exit_code: int

    def value(self, metric: str, key: str) -> float | None:
        values = (self.metrics.get(metric) or {}).get("values") or {}
        found = values.get(key)
        return float(found) if isinstance(found, int | float) else None

    @property
    def requests(self) -> float:
        return self.value("http_reqs", "count") or 0.0

    @property
    def throughput(self) -> float:
        rate = self.value("http_reqs", "rate")
        return rate if rate is not None else (self.requests / self.seconds if self.seconds else 0.0)

    @property
    def failed(self) -> float:
        """Share of responses k6 counted as failures, 0..1."""
        return self.value("http_req_failed", "rate") or 0.0

    @property
    def unexpected(self) -> float:
        return self.value("tollgate_unexpected", "count") or 0.0


def docker_available() -> None:
    if shutil.which("docker") is None:
        raise SystemExit("Docker is not on PATH. k6 runs in a container; nothing to run it with.")


def run_k6(
    script: str,
    name: str,
    gateway: str,
    key: str,
    env: dict[str, str],
    results: pathlib.Path,
    quiet: bool,
) -> K6Run:
    """One k6 invocation. The script writes /results/<name>.json; this reads it back."""
    inside = {
        "TOLLGATE_URL": gateway.replace("localhost", "host.docker.internal").replace(
            "127.0.0.1", "host.docker.internal"
        ),
        "TOLLGATE_KEY": key,
        **env,
    }
    command = [
        "docker",
        "run",
        "--rm",
        # A no-op on Docker Desktop, which defines the name already; needed on Linux.
        "--add-host=host.docker.internal:host-gateway",
        "-v",
        f"{SCRIPTS}:/scripts:ro",
        "-v",
        f"{results}:/results",
    ]
    for variable, value in inside.items():
        command += ["-e", f"{variable}={value}"]
    command += [K6_IMAGE, "run", "--quiet" if quiet else "--no-color", f"/scripts/{script}"]

    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=quiet, text=True, check=False)
    seconds = time.perf_counter() - started

    summary = results / f"{name}.json"
    if not summary.exists():
        if quiet and completed.stderr:
            print(completed.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"k6 wrote no summary for {name}; it exited {completed.returncode}.")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    return K6Run(
        name=name,
        metrics=payload.get("metrics") or {},
        meta=payload.get("meta") or {},
        seconds=seconds,
        exit_code=completed.returncode,
    )


# ---------------------------------------------------------------------------
# Environment the scenarios need
# ---------------------------------------------------------------------------


async def resolve_tenant(sessionmaker: Sessionmaker, key: str) -> Tenant:
    async with sessionmaker() as session:
        tenant = await session.scalar(
            select(Tenant)
            .join(ApiKey, ApiKey.tenant_id == Tenant.id)
            .where(ApiKey.key_hash == hash_key(key), ApiKey.revoked_at.is_(None))
        )
    if tenant is None:
        raise SystemExit("That key does not belong to a live tenant.")
    return tenant


async def clear_cache(sessionmaker: Sessionmaker, tenant_id: uuid.UUID) -> None:
    async with sessionmaker() as session:
        await session.execute(delete(CacheEntry).where(CacheEntry.tenant_id == tenant_id))
        await session.commit()


def recreate_mock(error_rate: float, latency_ms: int) -> None:
    """Restart the mock with a failure rate, for the degraded scenario.

    Random failure is what a degraded provider looks like, and it is the only kind the
    gateway's retry can do anything about: a `[[mock:error=429]]` directive travels in the
    request body, so every retry carries it and fails identically.
    """
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "--force-recreate", "mock-upstream"],
        check=True,
        env={
            **os.environ,
            "MOCK_ERROR_RATE": str(error_rate),
            "MOCK_LATENCY_MS": str(latency_ms),
        },
    )


def probe(gateway: str, key: str) -> None:
    """One real request, so the histograms exist before anything tries to read them.

    An instrument that has recorded nothing is absent from the exposition rather than
    present and zero, so without this the check below cannot tell a gateway that is not
    instrumented from one that is merely idle.
    """
    body = {
        "contents": [{"role": "user", "parts": [{"text": "preflight"}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 32},
    }
    try:
        httpx.post(
            f"{gateway}/v1beta/models/gemini-3.7-flash:generateContent",
            json=body,
            headers={"x-goog-api-key": key},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(f"The gateway would not serve a single request: {exc}") from exc


def drain_stale_connections(gateway: str, key: str, attempts: int = 12) -> None:
    """Send and discard a few requests after the mock has been replaced.

    The gateway keeps a pool of keep-alive connections to the upstream, and recreating
    the container leaves every one of them pointing at something that is gone. The first
    requests afterwards fail on a reset socket, and a benchmark that started measuring
    immediately would record those as the degraded upstream's doing when they are the
    benchmark's own. A provider replacing a host mid-flight is a real failure mode and
    worth its own test one day; it is not the one this scenario is for, so it is drained
    off here rather than left to contaminate it.
    """
    body = {
        "contents": [{"role": "user", "parts": [{"text": "drain"}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 32},
    }
    path = "/v1beta/models/gemini-3.7-flash:generateContent"
    with httpx.Client(base_url=gateway, timeout=30.0) as client:
        for _ in range(attempts):
            try:
                client.post(path, json=body, headers={"x-goog-api-key": key})
            except httpx.HTTPError:
                continue


def preflight(gateway: str, tenant: Tenant, key: str, top_rate: int) -> None:
    try:
        ready = httpx.get(f"{gateway}/readyz", timeout=10.0)
    except httpx.HTTPError as exc:
        raise SystemExit(f"The gateway is not answering on {gateway}: {exc}") from exc
    if ready.status_code != 200:
        raise SystemExit(
            f"The gateway is not ready ({ready.status_code}); it would fail every call."
        )

    # `/metrics` answering is not the same as the gateway recording anything. With
    # METRICS_ENDPOINT_ENABLED on but OTEL_ENABLED off, the endpoint serves the process's
    # default Python metrics and none of the gateway's, because no meter provider was
    # ever installed - so every overhead column would come back empty after a run that
    # otherwise looked perfect. Checking it here rather than discovering it in the report.
    probe(gateway, key)
    if OVERHEAD_METRIC not in scrape(gateway):
        raise SystemExit(
            f"The gateway is serving /metrics but not recording {OVERHEAD_METRIC}, so there\n"
            "would be no overhead to report. Both settings are needed, and OTEL_ENABLED is\n"
            "the one that installs the meter provider:\n\n"
            "    OTEL_ENABLED=True METRICS_ENDPOINT_ENABLED=True uv run uvicorn tollgate.main:app\n"
        )

    rpm = tenant.rate_limit_rpm
    if rpm is not None and rpm < top_rate * 60:
        raise SystemExit(
            f"Tenant '{tenant.name}' is limited to {rpm} requests a minute, and this sweep\n"
            f"offers up to {top_rate * 60}. The ceiling it found would be the limiter's, not\n"
            f"the gateway's. Exempt it first:\n\n"
            f"    uv run tollgate set-limits {tenant.name} --unlimited\n"
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}ms"


def estimate(point: Estimate | None) -> str:
    """A percentile from a bucketed histogram, printed to the precision it has."""
    return "-" if point is None else str(point)


@dataclass
class Scenario:
    """One k6 run, plus what the gateway's histogram said while it ran."""

    run: K6Run
    overhead: Overhead


def sweep_table(rows: list[Scenario]) -> str:
    """Client, gateway, upstream and overhead side by side, per offered rate.

    The upstream column is here because without it the table cannot be read. The mock is
    a single Python process too, and if it is the thing that slowed down then the
    gateway's rising total is a consequence rather than a cause.
    """
    lines = [
        f"{'offered':>8} {'achieved':>9} {'requests':>9} {'errors':>7} "
        f"{'client p50':>11} {'upstream mean':>14} "
        f"{'overhead mean':>14} {'overhead p50':>13} {'overhead p99':>13}",
    ]
    for row in rows:
        offered = row.run.meta.get("rate", "?")
        lines.append(
            f"{offered:>8} {row.run.throughput:>8.1f}/s {row.run.requests:>9.0f} "
            f"{row.run.failed * 100:>6.1f}% "
            f"{ms(row.run.value('http_req_duration', 'med')):>11} "
            f"{ms(row.overhead.upstream.mean):>14} "
            f"{ms(row.overhead.overhead.mean):>14} "
            f"{estimate(row.overhead.overhead.percentile(50)):>13} "
            f"{estimate(row.overhead.overhead.percentile(99)):>13}"
        )
    return "\n".join(lines)


def phase_table(run: K6Run) -> str:
    lines = [f"{'phase':>8} {'requests':>9} {'p50':>9} {'p95':>9} {'p99':>9}"]
    for phase in ("before", "spike", "after"):
        duration = f"http_req_duration{{phase:{phase}}}"
        count = run.value(f"http_reqs{{phase:{phase}}}", "count")
        lines.append(
            f"{phase:>8} {count or 0:>9.0f} "
            f"{ms(run.value(duration, 'med')):>9} "
            f"{ms(run.value(duration, 'p(95)')):>9} "
            f"{ms(run.value(duration, 'p(99)')):>9}"
        )
    return "\n".join(lines)


MEANINGS = {
    "0": "no status at all - timed out or reset",
    "200": "served",
    "402": "over budget",
    "403": "refused by policy",
    "429": "rate limited, the provider's or the gateway's",
    "500": "upstream error",
    "502": "upstream unreachable",
    "503": "upstream overloaded, or the gateway out of pool",
    "504": "upstream timed out",
}


def status_table(run: K6Run) -> str:
    """Every status the caller saw, and how many. Zero rows are left out."""
    lines = []
    for status in ("0", "200", "400", "401", "402", "403", "429", "500", "502", "503", "504"):
        count = run.value(f"tollgate_status{{status:{status}}}", "count")
        if not count:
            continue
        share = count / run.requests * 100 if run.requests else 0.0
        lines.append(f"{status:>8} {count:>9.0f} {share:>7.1f}%   {MEANINGS.get(status, '')}")
    return "\n".join(lines) if lines else "  (no statuses were broken out)"


def buckets_note(histogram: Histogram) -> str:
    bounds = sorted(b for b in histogram.buckets if b != float("inf"))
    return ", ".join(f"{b:g}" for b in bounds)


def report(
    sweep: list[Scenario],
    burst: Scenario | None,
    degraded: Scenario | None,
    steady_reference: Scenario | None,
    cache: tuple[Scenario, Scenario] | None,
    mock_latency_ms: int,
    degraded_error_rate: float,
) -> str:
    out: list[str] = []
    out.append(
        "Load generated by k6 in Docker against the gateway on this machine, with the\n"
        f"mock upstream holding every request for {mock_latency_ms} ms. Total latency below is\n"
        "what the client saw and is therefore mostly the mock; overhead is the gateway's\n"
        "own histogram (total minus upstream, per request), scraped either side of each\n"
        "run and diffed. That is the same series the dashboard's overhead panel reads and\n"
        "the same one the alert watches, so this benchmark and the alert cannot disagree\n"
        "about what overhead means."
    )
    if sweep:
        out.append(f"\nOverhead bucket bounds (ms): {buckets_note(sweep[0].overhead.overhead)}")

    if sweep:
        out.append("\n\nSTEADY LOAD, swept for a ceiling\n")
        out.append(sweep_table(sweep))
        out.append(
            "\n'offered' is the arrival rate k6 was told to hold; 'achieved' is what it\n"
            "managed. Load is offered at a rate rather than by a fixed pool of workers,\n"
            "because a pool that waits for each reply slows down when the service does and\n"
            "reports its own patience as the service's capacity.\n\n"
            "'client p50' is what k6 saw from inside its container, and is mostly the mock.\n"
            "'upstream mean' is what the gateway waited on, and is the column that says\n"
            "whose fault a bad row is. The mock is a single Python process on the same\n"
            "laptop as the gateway, the database and the load generator, so it saturates\n"
            "too: a row where both columns climb together is the machine running out, not\n"
            "the gateway. Only a row where overhead climbs and the upstream does not is a\n"
            "statement about this code.\n\n"
            "The overhead percentiles are read off a bucketed histogram, so they are printed\n"
            "as the bucket they landed in wherever the bucket is wide relative to the value.\n"
            "The mean beside them is not an estimate: it is the histogram's own sum over its\n"
            "own count, and it is the number to compare between rows."
        )

    if burst is not None:
        meta = burst.run.meta
        out.append(
            f"\n\nBURST, {meta.get('base_rate')}/s for {meta.get('quiet_seconds')}s, "
            f"{meta.get('spike_rate')}/s for {meta.get('spike_seconds')}s, then quiet again\n"
        )
        out.append(phase_table(burst.run))
        out.append(
            "\nThe row that matters is 'after' against 'before'. A gateway that queued the\n"
            "spike rather than shedding it is still working through the queue long after the\n"
            "spike ended, and the caller sees a service that never came back."
        )

    if degraded is not None:
        meta = degraded.run.meta
        out.append(
            f"\n\nDEGRADED UPSTREAM, {int(degraded_error_rate * 100)}% of upstream calls failing, "
            f"{int(float(meta.get('slow_share', 0)) * 100)}% of requests asking for "
            f"{meta.get('slow_ms')} ms\n"
        )
        rows = [f"{'':>22} {'requests':>9} {'failed':>8} {'unexpected':>11} {'overhead p99':>13}"]
        if steady_reference is not None:
            rows.append(
                f"{'healthy upstream':>22} {steady_reference.run.requests:>9.0f} "
                f"{steady_reference.run.failed * 100:>7.1f}% "
                f"{steady_reference.run.unexpected:>11.0f} "
                f"{estimate(steady_reference.overhead.overhead.percentile(99)):>13}"
            )
        rows.append(
            f"{'degraded upstream':>22} {degraded.run.requests:>9.0f} "
            f"{degraded.run.failed * 100:>7.1f}% {degraded.run.unexpected:>11.0f} "
            f"{estimate(degraded.overhead.overhead.percentile(99)):>13}"
        )
        out.append("\n".join(rows))
        out.append("\nWhat the caller got back, by status:\n")
        out.append(status_table(degraded.run))
        out.append(
            "\n'failed' is what the caller saw, after the gateway's retries had already\n"
            "absorbed what they could. 'unexpected' counts statuses outside the set a\n"
            "degraded upstream is allowed to produce, and is the number that should be zero:\n"
            "the upstream failing is not the gateway failing. Status 0 is k6's code for a\n"
            "request that got no status at all - a timeout or a reset - which is the one\n"
            "outcome a gateway is supposed to prevent, because it is the one the caller\n"
            "cannot tell apart from the gateway being down."
        )

    if cache is not None:
        cold, warm = cache
        out.append("\n\nCACHE, the same work cold and warm\n")
        rows = [f"{'':>6} {'requests':>9} {'hit rate':>9} {'wall clock':>11} {'p50':>9} {'p95':>9}"]
        for label, run in (("cold", cold.run), ("warm", warm.run)):
            hit = run.value("tollgate_cache_hit", "rate")
            rows.append(
                f"{label:>6} {run.requests:>9.0f} "
                f"{(hit * 100 if hit is not None else 0):>8.1f}% "
                f"{run.seconds:>10.1f}s "
                f"{ms(run.value('http_req_duration', 'med')):>9} "
                f"{ms(run.value('http_req_duration', 'p(95)')):>9}"
            )
        out.append("\n".join(rows))
        out.append(
            "\nIdentical prompts in an identical order, the same number of workers both\n"
            "times, with the tenant's entries deleted before the cold run. This is the one\n"
            "scenario driven by a fixed amount of work rather than an arrival rate, because\n"
            "the comparison is how long the same work took."
        )

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def write_report(path: str, text: str) -> None:
    out = pathlib.Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {out}")


async def main_async(args: argparse.Namespace) -> None:
    docker_available()
    settings = get_settings()
    engine = create_async_engine(args.database_url or settings.database_url, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        tenant = await resolve_tenant(sessionmaker, args.tenant_key)
        wanted = [int(r) for r in args.rates]
        preflight(args.gateway, tenant, args.tenant_key, max(wanted) if wanted else args.rate)

        with tempfile.TemporaryDirectory(prefix="tollgate-load-") as directory:
            results = pathlib.Path(directory)

            def measured(script: str, name: str, env: dict[str, str]) -> Scenario:
                before = scrape(args.gateway)
                run = run_k6(script, name, args.gateway, args.tenant_key, env, results, args.quiet)
                after = scrape(args.gateway)
                return Scenario(run=run, overhead=Overhead.between(before, after, tenant.name))

            sweep: list[Scenario] = []
            steady_reference: Scenario | None = None
            if "steady" in args.scenarios:
                for rate in wanted:
                    print(f"steady: {rate}/s for {args.duration}s")
                    scenario = measured(
                        "steady.js",
                        "steady",
                        {"RATE": str(rate), "DURATION": f"{args.duration}s"},
                    )
                    sweep.append(scenario)
                    if rate == args.rate:
                        steady_reference = scenario

            burst: Scenario | None = None
            if "burst" in args.scenarios:
                print("burst: quiet, spike, quiet")
                burst = measured(
                    "burst.js",
                    "burst",
                    {"RATE": str(args.rate), "SPIKE": str(args.spike)},
                )

            degraded: Scenario | None = None
            if "degraded" in args.scenarios:
                print(
                    f"degraded: restarting the mock with a {args.error_rate:.0%} failure rate",
                    flush=True,
                )
                recreate_mock(args.error_rate, args.mock_latency_ms)
                try:
                    drain_stale_connections(args.gateway, args.tenant_key)
                    degraded = measured(
                        "degraded.js",
                        "degraded",
                        {"RATE": str(args.rate), "DURATION": f"{args.duration}s"},
                    )
                finally:
                    print("degraded: restoring the mock", flush=True)
                    recreate_mock(0.0, args.mock_latency_ms)
                    drain_stale_connections(args.gateway, args.tenant_key)

            cache: tuple[Scenario, Scenario] | None = None
            if "cache" in args.scenarios:
                run_id = f"load{args.seed}"
                print("cache: clearing the tenant's entries, then cold")
                await clear_cache(sessionmaker, tenant.id)
                cold = measured(
                    "cache.js",
                    "cache_cold",
                    {"PHASE": "cold", "POOL": str(args.pool), "RUN_ID": run_id},
                )
                print("cache: warm, the same prompts again")
                warm = measured(
                    "cache.js",
                    "cache_warm",
                    {"PHASE": "warm", "POOL": str(args.pool), "RUN_ID": run_id},
                )
                cache = (cold, warm)

        text = report(
            sweep, burst, degraded, steady_reference, cache, args.mock_latency_ms, args.error_rate
        )
        print("\n" + text)
        if args.out:
            write_report(args.out, text)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tenant_key")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["steady", "burst", "degraded", "cache"],
        choices=["steady", "burst", "degraded", "cache"],
    )
    parser.add_argument(
        "--rates",
        nargs="+",
        default=[20, 40, 60, 80, 100, 120],
        help="arrival rates to sweep for the ceiling, requests per second",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=40,
        help="the rate burst and degraded run at, and the sweep row it is compared with",
    )
    # Forty-five seconds, not the fifteen this started at. Short runs on a laptop that is
    # also hosting the gateway, the mock, Postgres and the load generator produced a sweep
    # that was not monotonic - 60/s looking worse than 70/s - which is scheduling noise
    # reading as a result. At this length the curve is flat until it bends and stays bent.
    parser.add_argument("--duration", type=int, default=45, help="seconds per steady run")
    parser.add_argument("--spike", type=int, default=4, help="burst multiple over --rate")
    parser.add_argument(
        "--error-rate", type=float, default=0.2, help="upstream failure rate when degraded"
    )
    parser.add_argument("--mock-latency-ms", type=int, default=100)
    parser.add_argument("--pool", type=int, default=400, help="distinct prompts in the cache run")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--out", default="bench/results/load_test.txt")
    parser.add_argument("--quiet", action="store_true", help="hide k6's own output")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
