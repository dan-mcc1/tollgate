from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The git commit this process was built from. Set by the deploy pipeline, reported by
    # /livez, so a smoke test can tell the new version from an old task still draining.
    app_version: str = "dev"

    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/example_db"
    upstream_base_url: str = "http://localhost:8001"
    # SecretStr prints as '**********', so the provider key can't leak through a log or repr.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.7-flash"
    mock_gemini: bool = True

    # Upstream HTTP client. Each timeout guards a different stage of a request.
    upstream_connect_timeout_s: float = 5.0  # opening the TCP + TLS connection
    upstream_write_timeout_s: float = 10.0  # sending the request body
    upstream_read_timeout_s: float = 60.0  # max silence between response bytes; models are slow
    upstream_pool_timeout_s: float = 5.0  # waiting for a free connection from the pool
    upstream_max_connections: int = 100
    upstream_max_keepalive_connections: int = 20

    # Retries on 429, 5xx and failed connections. Delays use exponential backoff with full
    # jitter: a random wait between 0 and min(max, base * 2**attempt).
    upstream_max_retries: int = 2
    upstream_backoff_base_s: float = 0.25
    upstream_backoff_max_s: float = 4.0

    # Longest a non-streaming request may take in total, including retries. Streaming
    # requests aren't capped: they keep sending, so the read timeout above governs them.
    # Keep this below the load balancer's idle timeout, so the gateway returns a clean
    # error instead of the load balancer cutting the connection.
    request_deadline_s: float = 120.0

    # Rate limiting. "memory" keeps a bucket per container, which multiplies a
    # tenant's effective limit by the number of containers; "redis" is the one that is
    # correct on more than one task. Both are kept so the difference can be measured.
    limiter_backend: Literal["memory", "redis"] = "memory"
    redis_url: SecretStr = SecretStr("")
    redis_connect_timeout_s: float = 1.0
    redis_command_timeout_s: float = 0.5
    # Allow the request when Redis can't answer. See the module docstring in limits.py.
    rate_limit_fail_open: bool = True

    # Budgets. A reservation is a lease: this is how long one survives a gateway that
    # dies mid-request, so it must comfortably exceed request_deadline_s.
    budget_lease_s: float = 300.0
    # What to assume a response will run to when the request names no maxOutputTokens.
    # Generous on purpose - it is reconciled to the real figure moments later.
    budget_default_max_output_tokens: int = 8192
    # The month counter outlives its month, so a request on the 1st still finds
    # December's total where it left it rather than reseeding from the ledger.
    budget_month_ttl_s: float = 40 * 24 * 60 * 60

    # How long a price inserted by another container can take to come into force here.
    # Prices change a few times a year, so a stale minute costs nothing and saves a
    # database round trip on every single request.
    price_refresh_s: float = 300.0

    # Readiness checks. Keep the timeouts below the load balancer's health check timeout.
    readiness_db_timeout_s: float = 2.0
    readiness_upstream_timeout_s: float = 3.0
    readiness_upstream_cache_s: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
