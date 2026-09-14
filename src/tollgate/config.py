from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/example_db"
    upstream_base_url: str = "http://localhost:8001"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    mock_gemini: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
