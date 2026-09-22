from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration, read from environment variables (12-factor)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "duvo-api"
    environment: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    db_pool_size: int = 5
    db_timeout_s: float = 5.0

    redis_url: str = "redis://localhost:6379/0"
    redis_timeout_s: float = 2.0

    job_max_tries: int = 3
    job_timeout_s: int = 60
    worker_metrics_port: int = 9100
    chaos_failure_rate: float = 0.0  # 0..1 — makes the example job fail on purpose


@lru_cache
def get_settings() -> Settings:
    return Settings()
