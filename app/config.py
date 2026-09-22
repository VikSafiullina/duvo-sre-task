from functools import lru_cache
from typing import Self

from pydantic import model_validator
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
    # Our own bound on the launch, inside arq's job_timeout: a slow launch then takes the
    # retry -> failed path. arq's timeout cancels the job instead, leaving it "starting".
    launch_timeout_s: float = 45.0
    worker_drain_s: int = 8  # on SIGTERM, let in-flight jobs finish (SIGKILL follows at 10s)
    worker_metrics_port: int = 9100
    chaos_failure_rate: float = 0.0  # 0..1 — makes sandbox starts fail on purpose

    @model_validator(mode="after")
    def _launch_fits_in_job(self) -> Self:
        # headroom for the handler's own DB writes after the launch gives up
        if self.launch_timeout_s + 2 * self.db_timeout_s > self.job_timeout_s:
            raise ValueError("launch_timeout_s + 2 * db_timeout_s must fit in job_timeout_s")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
