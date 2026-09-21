import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tollgate.auth import TenantContext
from tollgate.config import get_settings
from tollgate.errors import GatewayError, handle_gateway_error
from tollgate.health import UpstreamProbe
from tollgate.health import router as health_router
from tollgate.limits import build_budget_guard, build_rate_limiter, build_redis, rate_limited
from tollgate.logs import RequestContextMiddleware
from tollgate.proxy.passthrough import create_upstream_client, proxy_generate_content
from tollgate.proxy.streaming import proxy_stream_generate_content
from tollgate.telemetry import facts, setup_telemetry
from tollgate.usage import MONTH_FORMAT, PriceBook, format_usd, rollup


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Create shared resources once at startup and close them at shutdown."""
    settings = get_settings()
    app.state.settings = settings
    # Before anything else that might want to emit a span.
    telemetry = setup_telemetry(settings)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.state.pricebook = PriceBook(app.state.sessionmaker, settings.price_refresh_s)

    # One client for the whole process. It keeps connections to the upstream open and
    # reuses them; a client per request would redo the TCP and TLS handshake every time.
    http_client = create_upstream_client(settings)
    app.state.http_client = http_client
    app.state.upstream_probe = UpstreamProbe(cache_s=settings.readiness_upstream_cache_s)
    # One Redis client for the limiter and the budget cache alike, closed in one place.
    redis = build_redis(settings)
    app.state.redis = redis
    app.state.rate_limiter = build_rate_limiter(settings, redis)
    app.state.budget = build_budget_guard(
        settings, app.state.sessionmaker, app.state.pricebook, redis
    )

    try:
        yield
    finally:
        await http_client.aclose()
        if redis is not None:
            await redis.aclose()
        await engine.dispose()
        telemetry.shutdown()  # flush whatever spans are still batched


app = FastAPI(title="Tollgate", lifespan=lifespan)
app.add_exception_handler(GatewayError, handle_gateway_error)
app.add_middleware(RequestContextMiddleware)
app.include_router(health_router)


VALID_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus scrape endpoint, for local development.

    It carries tenant names and their spend, and the load balancer in front of this is
    public, so it is off unless `METRICS_ENDPOINT_ENABLED` says otherwise and stays off
    in production. Production pushes the same numbers over OTLP, which needs no inbound
    route at all.
    """
    if not request.app.state.settings.metrics_endpoint_enabled:
        raise GatewayError(404, "not_found", "Metrics endpoint is not enabled.")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def money(microcents: int) -> dict[str, Any]:
    """Both forms, always. The integer is what the ledger holds and what a caller should
    do arithmetic on; the string is for people, and rounding it is safe because nothing
    downstream adds it up."""
    return {"microcents": microcents, "usd": format_usd(microcents)}


@app.get("/v1/spend")
async def spend(
    request: Request,
    tenant: Annotated[TenantContext, Depends(rate_limited)],
    month: str | None = None,
) -> dict[str, Any]:
    """What this tenant has spent, by model. A tenant can only ever read its own, because
    the only tenant it can name is the one its key resolves to.

    The figures come from the ledger, not from the budget cache, so they are what the
    tenant will be billed rather than what is currently reserved. Requests still in
    flight are therefore not counted here; they are counted when deciding whether to
    admit the next one.
    """
    facts().method = "spend"  # real tenant traffic, and it should say so on a panel
    month = month or datetime.now(UTC).strftime(MONTH_FORMAT)
    if not VALID_MONTH.match(month):
        raise GatewayError(400, "invalid_month", "month must look like 2026-09.")

    totals = await rollup(request.app.state.sessionmaker, tenant.tenant_id, month)
    budget = tenant.monthly_budget_microcents
    return {
        "tenant": tenant.tenant_name,
        "month": totals.month,
        "requests": totals.requests,
        "spend": money(totals.cost_microcents),
        "budget": money(budget) if budget is not None else None,
        "remaining": money(max(0, budget - totals.cost_microcents)) if budget is not None else None,
        "by_model": [
            {
                "model": entry.model,
                "requests": entry.requests,
                "input_tokens": entry.input_tokens,
                "output_tokens": entry.output_tokens,
                "thoughts_tokens": entry.thoughts_tokens,
                "spend": money(entry.cost_microcents),
            }
            for entry in totals.by_model
        ],
    }


@app.post("/v1beta/models/{model_method}", response_model=None)
async def model_method(
    model_method: str,
    request: Request,
    tenant: Annotated[TenantContext, Depends(rate_limited)],
) -> Response:
    model, _, method = model_method.partition(":")
    if method == "generateContent":
        return await proxy_generate_content(request, tenant, model, method)
    if method == "streamGenerateContent":
        return await proxy_stream_generate_content(request, tenant, model, method)
    raise GatewayError(501, "unsupported_method", f"'{method}' is not supported yet.")
