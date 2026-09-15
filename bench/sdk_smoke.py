"""Call Tollgate with Google's official SDK, unchanged except for the base URL.

uv run --with google-genai python bench/sdk_smoke.py tg_your_tenant_key
"""

import sys

from google import genai

GATEWAY_URL = "http://localhost:8000"
MODEL = "gemini-3.7-flash"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    client = genai.Client(api_key=sys.argv[1], http_options={"base_url": GATEWAY_URL})
    response = client.models.generate_content(model=MODEL, contents="Say hello in five words.")
    print(response.text)
    print(response.usage_metadata)


if __name__ == "__main__":
    main()
