"""Record which dashboard the committed screenshots were taken from.

    uv run python dashboards/manifest.py

A screenshot is the one artefact in this repository that cannot be regenerated from it,
so something has to fail when the dashboard changes and the pictures do not. The obvious
check - is the PNG newer than the JSON - cannot work: git does not store modification
times, so on a fresh checkout every file is written within the same millisecond, in
whatever order the checkout happens to use. That check passed locally and was decided by
alphabetical accident in CI.

So the link is recorded instead of inferred. This writes the hash of the dashboard at the
moment the screenshots were accepted, and tests/test_dashboard.py fails when the live
hash no longer matches. Editing a panel and forgetting to retake the pictures is then a
failing build rather than a README quietly showing a dashboard that no longer exists.
"""

import hashlib
import json
import pathlib

HERE = pathlib.Path(__file__).parent
DASHBOARD = HERE / "tollgate.json"
MANIFEST = HERE / "screenshots.json"


def dashboard_digest() -> str:
    """The dashboard as one hash, over what it *means* rather than how it is written.

    Parsed and re-serialised canonically rather than hashed as raw bytes. Hashing the
    bytes is the obvious thing and it is wrong here: `.gitattributes` sets `eol=lf`, so
    the repository stores LF and a Windows checkout gets CRLF, and the same dashboard
    then hashes differently on the machine that took the screenshots than on the runner
    that checks them. That is a test which passes locally and fails in CI forever.

    The trade is that reformatting the file - reindenting, reordering keys - no longer
    counts as a change. That is the right way round: a screenshot shows what the panels
    look like, and neither of those alters a single pixel.
    """
    content = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def screenshots() -> list[str]:
    return sorted(path.name for path in HERE.glob("*.png"))


def write() -> None:
    MANIFEST.write_text(
        json.dumps(
            {
                "dashboard": DASHBOARD.name,
                "dashboard_sha256": dashboard_digest(),
                "screenshots": screenshots(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Recorded {len(screenshots())} screenshot(s) against {dashboard_digest()[:12]}")


if __name__ == "__main__":
    write()
