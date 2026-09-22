from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import Deployment


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

    # Worker only: which pool this process is and what it runs. Each pool has its own queue.
    deployment: Deployment = Deployment.STABLE
    version: str = "dev"

    job_max_tries: int = 3
    job_timeout_s: int = 60
    worker_metrics_port: int = 9100
    chaos_failure_rate: float = 0.0  # 0..1 — makes sandbox starts fail on purpose

    # Sandbox runtime (the worker talks to the local Docker daemon)
    docker_timeout_s: float = 10.0  # per Docker API call
    sandbox_image: str = "traefik/whoami:v1.11"  # pinned: a moving tag would change under us
    sandbox_port: int = 8080
    sandbox_network: str = "duvo-sandboxes"  # isolated from Postgres/Redis on purpose
    sandbox_public_host: str = "localhost"  # host part of the URL handed back to callers
    sandbox_memory: str = "64m"
    sandbox_cpus: float = 0.25
    sandbox_pids: int = 64
    sandbox_ready_timeout_s: float = 15.0  # container started != server answering
    sandbox_max_active: int = 50  # admission control: protects the host from bursts
    reconcile_interval_s: int = 30  # TTL reaper / orphan sweep cadence


@lru_cache
def get_settings() -> Settings:
    return Settings()
