import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tollgate.config import Settings
from tollgate.health import UpstreamProbe
from tollgate.main import app


async def test_livez_needs_no_dependencies() -> None:
    # No database, no upstream, no lifespan: liveness must still answer.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tollgate") as client:
        response = await client.get("/livez")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert response.json()["version"]  # "dev" locally, the git SHA when deployed


async def test_ready_when_database_and_upstream_are_reachable(gateway: httpx.AsyncClient) -> None:
    response = await gateway.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "upstream": "ok"}}


class Upstream:
    def __init__(self, result: httpx.Response | Exception) -> None:
        self.result = result
        self.calls = 0
        self.headers: list[httpx.Headers] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.headers.append(request.headers)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def upstream() -> Upstream:
    return Upstream(httpx.Response(200))


@pytest.fixture
def upstream_transport(upstream: Upstream) -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(upstream.handle)


async def test_upstream_errors_do_not_make_the_task_unready(
    gateway: httpx.AsyncClient, upstream: Upstream
) -> None:
    # A provider outage must not get healthy tasks killed and restarted by ECS.
    upstream.result = httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})

    response = await gateway.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["upstream"] == "ok"


async def test_unreachable_upstream_makes_the_task_unready(
    gateway: httpx.AsyncClient, upstream: Upstream
) -> None:
    upstream.result = httpx.ConnectError("no route to host")

    response = await gateway.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "upstream": "ConnectError"},
    }


async def test_upstream_probe_sends_no_credentials(
    gateway: httpx.AsyncClient, upstream: Upstream
) -> None:
    await gateway.get("/readyz")

    [headers] = upstream.headers
    assert "x-goog-api-key" not in headers


async def test_unreachable_database_makes_the_task_unready(
    gateway: httpx.AsyncClient, settings: Settings
) -> None:
    dead = create_async_engine(
        "postgresql+asyncpg://postgres:password@127.0.0.1:1/nothing", poolclass=NullPool
    )
    app.state.sessionmaker = async_sessionmaker(dead)
    app.state.settings = settings.model_copy(update={"readiness_db_timeout_s": 1.0})

    response = await gateway.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] != "ok"
    await dead.dispose()


async def test_upstream_probe_result_is_cached() -> None:
    upstream = Upstream(httpx.Response(404))
    probe = UpstreamProbe(cache_s=60)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream.handle), base_url="http://upstream"
    ) as client:
        for _ in range(5):
            assert await probe.check(client, timeout_s=1) is None

    assert upstream.calls == 1
