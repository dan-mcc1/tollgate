"""Phase 1 baseline: how much latency the gateway adds over calling the upstream directly.

Sends the same request to the upstream and through the gateway, alternating so both see
the same conditions, and reports percentiles. Run it against the mock so the upstream's
own latency is steady and the difference is the gateway's overhead.

    docker compose up -d --wait
    uv run uvicorn tollgate.main:app --port 8000        # in another terminal
    uv run python bench/baseline_latency.py tg_your_tenant_key --requests 500
"""

import argparse
import statistics
import time

import httpx

BODY = {"contents": [{"role": "user", "parts": [{"text": "Say hello."}]}]}
PATH = "/v1beta/models/gemini-3.7-flash:generateContent"


def percentile(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
    return ordered[index]


def timed_post(client: httpx.Client, key: str) -> float:
    started = time.perf_counter()
    response = client.post(PATH, json=BODY, headers={"x-goog-api-key": key})
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return elapsed_ms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_key")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--upstream", default="http://localhost:8001")
    parser.add_argument("--upstream-key", default="bench", help="any key works for the mock")
    args = parser.parse_args()

    direct: list[float] = []
    via_gateway: list[float] = []
    with (
        httpx.Client(base_url=args.upstream) as upstream,
        httpx.Client(base_url=args.gateway) as gateway,
    ):
        for _ in range(args.warmup):
            timed_post(upstream, args.upstream_key)
            timed_post(gateway, args.tenant_key)
        for _ in range(args.requests):
            direct.append(timed_post(upstream, args.upstream_key))
            via_gateway.append(timed_post(gateway, args.tenant_key))

    print(f"{args.requests} sequential requests each, after {args.warmup} warmup\n")
    print(f"{'':>10} {'direct':>10} {'gateway':>10} {'added':>10}")
    for label, p in (("p50", 50.0), ("p95", 95.0), ("p99", 99.0)):
        d, g = percentile(direct, p), percentile(via_gateway, p)
        print(f"{label:>10} {d:>9.1f}ms {g:>9.1f}ms {g - d:>9.1f}ms")
    mean_d, mean_g = statistics.fmean(direct), statistics.fmean(via_gateway)
    print(f"{'mean':>10} {mean_d:>9.1f}ms {mean_g:>9.1f}ms {mean_g - mean_d:>9.1f}ms")


if __name__ == "__main__":
    main()
