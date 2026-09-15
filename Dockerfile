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
# Stage 2: runtime. Same Python base, no uv, no build cache, no source, not root.
# ---------------------------------------------------------------------------------------
FROM python:3.13-slim

# A fixed, unprivileged user. If the app is compromised, the attacker isn't root.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
# Migrations ship in the image so the deploy can run `alembic upgrade head` with the
# exact code being deployed.
COPY alembic.ini logging.json ./
COPY migrations ./migrations

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# --proxy-headers: trust X-Forwarded-For/Proto from the load balancer, so logs and
# redirects see the real client and https. Safe because only the ALB can reach the task.
# --log-config: every line JSON from the first one, including uvicorn's own startup lines.
CMD ["uvicorn", "tollgate.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--log-config", "logging.json"]
