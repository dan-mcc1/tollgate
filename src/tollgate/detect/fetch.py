"""Download a pinned classifier, verify its bytes, and put it where the gateway looks.

    uv run python -m tollgate.detect.fetch tiny
    uv run python -m tollgate.detect.fetch deberta-base --dest models
    uv run python -m tollgate.detect.fetch tiny --print-digests

The Dockerfile runs this in a build stage, so the model is part of the image rather than
something a task downloads on boot. That choice is worth stating: fetching at startup would
keep the image small and make every cold start depend on a third party being reachable, and
a readiness probe that fails because a model host is having an afternoon is a bad trade for
a few hundred megabytes. Baked in, the image is the artefact and the deploy is reproducible.

**Nothing is trusted.** Every file is fetched at a commit sha, never a branch, and checked
against the SHA-256 in the registry before it is moved into place. A model is executable
input to a security control: "it downloaded successfully" is not the same as "it is the file
the published numbers were measured against". A mismatch deletes the download and fails.

`--print-digests` is how the registry's hashes were filled in, and how they would be
refreshed if a model were ever repinned to a new revision: it downloads, installs and
prints what it got, without checking it against anything. Every other run verifies.
"""

import argparse
import sys
import tempfile
from pathlib import Path

import httpx

from tollgate.detect.classifier import KNOWN_MODELS, ModelSpec, digest_of

# Public files, no credential. A gateway build that needed a token to fetch its own detector
# would be a build nobody else could reproduce.
BASE_URL = "https://huggingface.co"
TIMEOUT_S = 120.0


def url_for(spec: ModelSpec, path: str) -> str:
    return f"{BASE_URL}/{spec.repo}/resolve/{spec.revision}/{path}"


def download(client: httpx.Client, url: str, destination: Path) -> None:
    """Stream one file to disk. Streamed because the larger graph is 738 MB and holding
    that in memory to write it out again is how a build container gets killed."""
    with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                handle.write(chunk)


def fetch(spec: ModelSpec, destination: Path, *, print_digests: bool = False) -> int:
    """Fetch every file of one model. Returns a process exit code."""
    directory = spec.directory(destination)
    directory.mkdir(parents=True, exist_ok=True)
    failures = 0

    with httpx.Client(timeout=TIMEOUT_S) as client:
        for name, (path, expected) in spec.files.items():
            target = directory / name
            # Downloaded beside the target and moved afterwards, so an interrupted fetch
            # leaves no half a graph for the gateway to load at the next start.
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                partial = Path(handle.name)
            try:
                print(f"{spec.name}/{name} <- {spec.repo}@{spec.revision[:12]}/{path}")
                download(client, url_for(spec, path), partial)
                actual = digest_of(partial)
                if print_digests:
                    print(f"    sha256 {actual}")
                elif expected and actual != expected:
                    # Deleted rather than kept for inspection: what is on disk is a file
                    # nobody can account for, and the one thing that must not happen is for
                    # it to be picked up by a later run that skips the download.
                    print(
                        f"    DIGEST MISMATCH\n      expected {expected}\n      got      {actual}"
                    )
                    failures += 1
                    continue
                elif not expected:
                    print(f"    sha256 {actual} (not pinned; run --print-digests to pin it)")
                partial.replace(target)
                # A temporary file is created 0600, and `replace` keeps its mode. Left like
                # that, a model fetched as root in a Docker build stage is unreadable to the
                # unprivileged user the gateway runs as, and the container fails to start.
                target.chmod(0o644)
                print(f"    {target} ({target.stat().st_size:,} bytes)")
            finally:
                partial.unlink(missing_ok=True)

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tollgate.detect.fetch")
    parser.add_argument("model", choices=sorted(KNOWN_MODELS), help="which classifier to fetch")
    parser.add_argument(
        "--dest",
        default="models",
        help="where model directories live; must match DETECTION_MODEL_DIR (default: models)",
    )
    parser.add_argument(
        "--print-digests",
        action="store_true",
        help="print each file's SHA-256 instead of checking it, for repinning the registry",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="fetch again even when the files are already present and verified",
    )
    args = parser.parse_args(argv)

    spec = KNOWN_MODELS[args.model]
    directory = spec.directory(args.dest)
    if not args.force and not args.print_digests and already_verified(spec, directory):
        print(f"{spec.name} is already in {directory} and matches the pinned digests.")
        return 0
    return fetch(spec, Path(args.dest), print_digests=args.print_digests)


def already_verified(spec: ModelSpec, directory: Path) -> bool:
    """Whether every pinned file is present with the right bytes.

    Hashing 738 MB takes a couple of seconds, which is worth paying to make a repeated
    build skip a 738 MB download - and worth paying to notice a truncated file that is the
    right length.
    """
    for name, (_, expected) in spec.files.items():
        path = directory / name
        if not path.is_file() or not expected or digest_of(path) != expected:
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
