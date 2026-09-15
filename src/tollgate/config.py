from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
