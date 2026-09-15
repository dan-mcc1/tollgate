"""Liveness and readiness.

/livez   Is the process up? No dependencies. If this fails, the container should be replaced.
/readyz  Can this task serve traffic? The load balancer routes only to tasks that pass.

On ECS, a task failing the load balancer health check is killed and replaced, so readiness
must only fail for problems a new task might fix, or that make serving truly impossible:

- Database unreachable: fail. Every request authenticates against it.
- Upstream unreachable at the network level (DNS, TCP, TLS): fail. Nothing can be proxied.
- Upstream reachable but returning errors (a Gemini outage, 429s, 503s): pass. Replacing
  tasks can't fix the provider, and a restart loop would turn a partial outage into a total
  one. Callers still get the upstream's own error, passed through.

The upstream probe is cached, so health checks every few seconds don't become a steady
stream of requests to the provider.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.config import Settings

logger = logging.getLogger("tollgate.health")
router = APIRouter()


@dataclass
class UpstreamProbe:
    """Remembers the last upstream reachability result for `cache_s` seconds."""

    cache_s: float
    checked_at: float = 0.0
    error: str | None = None
    has_result: bool = False

    async def check(self, client: httpx.AsyncClient, timeout_s: float) -> str | None:
        now = time.monotonic()
        if self.has_result and now - self.checked_at < self.cache_s:
            return self.error
        try:
            # Any HTTP response, even a 404 or a 503, proves DNS, TCP and TLS all work.
            # No API key is sent: this must never cost money or count against a quota.
            await client.get("/", timeout=timeout_s)
            self.error = None
        except httpx.HTTPError as exc:
            self.error = type(exc).__name__
        self.checked_at, self.has_result = now, True
        return self.error


async def check_database(
    sessionmaker: async_sessionmaker[AsyncSession], timeout_s: float
) -> str | None:
    try:
        async with asyncio.timeout(timeout_s), sessionmaker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # any failure at all means "not ready"
        return type(exc).__name__
    return None


@router.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    client: httpx.AsyncClient = request.app.state.http_client
    probe: UpstreamProbe = request.app.state.upstream_probe

    database_error, upstream_error = await asyncio.gather(
        check_database(sessionmaker, settings.readiness_db_timeout_s),
        probe.check(client, settings.readiness_upstream_timeout_s),
    )
    checks = {
        "database": database_error or "ok",
        "upstream": upstream_error or "ok",
    }
    ready = database_error is None and upstream_error is None
    if not ready:
        logger.warning("not ready", extra={"fields": {"checks": checks}})
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
