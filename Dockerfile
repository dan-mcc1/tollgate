# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------------------
# Stage 1: build. Has uv and a writable cache; installs dependencies and the app into a
# virtual environment. Nothing from this stage ships except that environment.
# ---------------------------------------------------------------------------------------
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /bin/uv

# Compile .pyc files now so the container doesn't do it on every cold start.
# Copy packages out of uv's cache instead of hard-linking (the cache is a separate mount).
# Use the image's Python, never a uv-downloaded one, so the runtime stage can run the venv.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, from the lockfile alone. This layer only rebuilds when
# pyproject.toml or uv.lock change, not on every code edit.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Then the app itself, installed as a regular (non-editable) package into the venv,
# so the runtime stage doesn't need the source tree.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------------------
# Stage 2: the classifier. Fetched at build time, at a pinned commit, and verified against
# the SHA-256 in detect/classifier.py before it is allowed into the image.
#
# Baked in rather than downloaded on boot. A task that fetched its own model would start
# faster to build and slower to trust: every cold start would depend on a model host being
# reachable, and a readiness probe failing because huggingface.co is having an afternoon is
# a bad trade for a few tens of megabytes. This way the image is the artefact, and two
# deploys of the same digest are inspecting with the same bytes.
#
# Its own stage, so the download is cached on the model name alone: editing application code
# does not re-fetch a graph, and the layer is copied into the runtime image without uv, the
# build cache or the fetcher coming with it.
# ---------------------------------------------------------------------------------------
FROM builder AS detector

# Which classifier ships, and empty means none - the regex baseline alone, which is what the
# default build runs. That default is a measurement rather than caution, and the measurement is
# in bench/results/detection_eval.txt:
#
#   * `tiny` is unusable. It flags 43% of the ordinary short prompts in app_prompts.jsonl -
#     "Summarise this.", "Fix this SQL." - and at a threshold where that rate is tolerable its
#     recall is no better than the regex baseline, which is free.
#   * `deberta-base` is the real thing: 0.938 precision, 0.666 recall, and it flags none of
#     those app prompts. It costs 739 MB of image and 35 ms at the median, 634 ms at the 99th.
#     Detection time is inside gateway overhead on purpose, so turning it on means the p99
#     overhead alert in infra/grafana has to be raised from 100 ms to roughly 750 ms - and an
#     alert at 750 ms is a much blunter instrument than one at 100 ms.
#
# So the classifier is opt-in per deployment, with the trade stated:
#
#   docker build --build-arg DETECTION_MODEL=deberta-base .
#
ARG DETECTION_MODEL=""

WORKDIR /models
# The builder's virtual environment, not the image's bare Python: the fetcher is part of the
# package, and its registry is where the revision and the digest are pinned.
RUN if [ -n "$DETECTION_MODEL" ]; then \
        /app/.venv/bin/python -m tollgate.detect.fetch "$DETECTION_MODEL" --dest /models; \
    else \
        echo "no classifier baked in; the regex baseline is what this image runs"; \
    fi

# ---------------------------------------------------------------------------------------
# Stage 3: runtime. Same Python base, no uv, no build cache, no source, not root.
# ---------------------------------------------------------------------------------------
FROM python:3.13-slim

# Apply Debian security updates released since the base image was last rebuilt (the
# official image lags behind by days to weeks), then drop the package index to keep the
# layer small. Then a fixed, unprivileged user: if the app is compromised, it isn't root.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
# Migrations ship in the image so the deploy can run `alembic upgrade head` with the
# exact code being deployed.
COPY alembic.ini logging.json ./
COPY migrations ./migrations
# Owned by root, so the unprivileged app user can read the graph and not replace it. The
# model is executable input to a security control, and nothing at runtime has any business
# rewriting it. (Not --chmod=444: that strips the execute bit from the directories too, and
# a directory nobody may traverse is a model nobody may load.)
COPY --from=detector --chown=root:root /models ./models

ARG DETECTION_MODEL=""

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Names the model the stage above fetched. Set here rather than left to the task
    # definition, so the image cannot be deployed configured for a model it does not carry -
    # which would mean refusing to start, because detect/service.py will not silently fall
    # back to the regex baseline.
    DETECTION_CLASSIFIER_MODEL=${DETECTION_MODEL} \
    DETECTION_MODEL_DIR=/app/models

USER app

EXPOSE 8000

# --proxy-headers: trust X-Forwarded-For/Proto from the load balancer, so logs and
# redirects see the real client and https. Safe because only the ALB can reach the task.
# --log-config: every line JSON from the first one, including uvicorn's own startup lines.
CMD ["uvicorn", "tollgate.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--log-config", "logging.json"]
