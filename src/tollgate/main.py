from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tollgate.auth import TenantContext, require_tenant
from tollgate.config import get_settings
from tollgate.errors import GatewayError, handle_gateway_error
from tollgate.proxy.passthrough import create_upstream_client, proxy_generate_content


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Create shared resources once at startup and close them at shutdown."""
    settings = get_settings()
    app.state.settings = settings

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    # One client for the whole process. It keeps connections to the upstream open and
    # reuses them; a client per request would redo the TCP and TLS handshake every time.
    http_client = create_upstream_client(settings)
    app.state.http_client = http_client

    try:
        yield
    finally:
        await http_client.aclose()
        await engine.dispose()


app = FastAPI(title="Tollgate", lifespan=lifespan)
app.add_exception_handler(GatewayError, handle_gateway_error)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1beta/models/{model_method}", response_model=None)
async def model_method(
    model_method: str,
    request: Request,
    tenant: Annotated[TenantContext, Depends(require_tenant)],
) -> Response:
    model, _, method = model_method.partition(":")
    if method != "generateContent":
        # streamGenerateContent arrives in phase 3.
        raise GatewayError(501, "unsupported_method", f"'{method}' is not supported yet.")
    return await proxy_generate_content(request, tenant, model, method)
