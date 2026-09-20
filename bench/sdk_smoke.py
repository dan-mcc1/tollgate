"""Call Tollgate with Google's official SDK, unchanged except for the base URL.

    uv run --with google-genai python bench/sdk_smoke.py tg_your_tenant_key
    uv run --with google-genai python bench/sdk_smoke.py tg_your_tenant_key --stream

--stream prints each chunk as it arrives, with the milliseconds since the request started,
so a stall or a buffering gateway is visible rather than inferred.
"""

import argparse
import time

from google import genai

GATEWAY_URL = "https://tollgate.danmccabe.dev"
MODEL = "gemini-3.7-flash"
# Long, and with thinking off. Gemini buffers the start of a response, so a short answer
# arrives as a single burst even with no gateway in the path, and a thinking model spends
# most of the wait before it has anything to send. Neither shows whether relaying works.
PROMPT = "Write a detailed 900 word essay about the history of computer networking."
NO_THINKING = {"thinking_config": {"thinking_budget": 0}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_key")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--url", default=GATEWAY_URL)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()

    client = genai.Client(api_key=args.tenant_key, http_options={"base_url": args.url})

    if not args.stream:
        response = client.models.generate_content(model=args.model, contents="Say hello.")
        print(response.text)
        print(response.usage_metadata)
        return

    started = time.perf_counter()
    first: float | None = None
    usage = None
    stream = client.models.generate_content_stream(
        model=args.model, contents=PROMPT, config=NO_THINKING
    )
    for chunk in stream:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if first is None:
            first = elapsed_ms
        if chunk.text:
            print(f"[{elapsed_ms:7.0f} ms] {chunk.text}", end="", flush=True)
        usage = chunk.usage_metadata or usage
    total_ms = (time.perf_counter() - started) * 1000

    print(f"\n\nfirst token after {first:.0f} ms, whole response in {total_ms:.0f} ms")
    print(usage)


if __name__ == "__main__":
    main()
