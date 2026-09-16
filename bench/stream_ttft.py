"""Phase 3 baseline: how much the gateway adds to time to first token.

Time to first token is what a user of a streaming app actually waits for; the rest of the
response arrives while they read. It is also the number that exposes a buffering gateway,
because buffering turns time-to-first-token into time-to-whole-response.

Alternates direct and proxied calls so both see the same conditions, and measures the
moment the first byte of the first event arrives.

    docker compose up -d --wait
    uv run uvicorn tollgate.main:app --port 8000        # in another terminal
    uv run python bench/stream_ttft.py tg_your_tenant_key --requests 100
"""

import argparse
import statistics
import time

import httpx

PATH = "/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse"
BODY = {"contents": [{"role": "user", "parts": [{"text": "Write a short paragraph."}]}]}


def percentile(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def measure(client: httpx.Client, key: str) -> tuple[float, float]:
    """Returns (time to first byte, total time) in milliseconds."""
    started = time.perf_counter()
    first: float | None = None
    with client.stream("POST", PATH, json=BODY, headers={"x-goog-api-key": key}) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            if chunk and first is None:
                first = (time.perf_counter() - started) * 1000
    total = (time.perf_counter() - started) * 1000
    if first is None:
        raise RuntimeError("the stream produced no bytes")
    return first, total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_key")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--upstream", default="http://localhost:8001")
    parser.add_argument("--upstream-key", default="bench", help="any key works for the mock")
    args = parser.parse_args()

    direct: list[float] = []
    proxied: list[float] = []
    direct_total: list[float] = []
    proxied_total: list[float] = []

    with (
        httpx.Client(base_url=args.upstream, timeout=60) as upstream,
        httpx.Client(base_url=args.gateway, timeout=60) as gateway,
    ):
        for _ in range(args.warmup):
            measure(upstream, args.upstream_key)
            measure(gateway, args.tenant_key)
        for _ in range(args.requests):
            first, total = measure(upstream, args.upstream_key)
            direct.append(first)
            direct_total.append(total)
            first, total = measure(gateway, args.tenant_key)
            proxied.append(first)
            proxied_total.append(total)

    print(f"{args.requests} streamed requests each, after {args.warmup} warmup\n")
    print("time to first token")
    print(f"{'':>10} {'direct':>10} {'gateway':>10} {'added':>10}")
    for label, p in (("p50", 50.0), ("p95", 95.0), ("p99", 99.0)):
        d, g = percentile(direct, p), percentile(proxied, p)
        print(f"{label:>10} {d:>9.1f}ms {g:>9.1f}ms {g - d:>9.1f}ms")
    mean_d, mean_g = statistics.fmean(direct), statistics.fmean(proxied)
    print(f"{'mean':>10} {mean_d:>9.1f}ms {mean_g:>9.1f}ms {mean_g - mean_d:>9.1f}ms")

    print("\nwhole response")
    print(f"{'':>10} {'direct':>10} {'gateway':>10} {'added':>10}")
    d50, g50 = percentile(direct_total, 50), percentile(proxied_total, 50)
    print(f"{'p50':>10} {d50:>9.1f}ms {g50:>9.1f}ms {g50 - d50:>9.1f}ms")

    # A gateway that buffered would show time-to-first-token close to whole-response time.
    ratio = statistics.fmean(proxied) / statistics.fmean(proxied_total)
    print(f"\nfirst token arrives {ratio:.0%} of the way through the response (lower is better)")


if __name__ == "__main__":
    main()
