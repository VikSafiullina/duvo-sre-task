import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from redis.asyncio import Redis

from app.config import Settings
from app.db import make_engine
from app.models import Base
from app.runtime import RuntimeSandbox


async def reset_state(settings: Settings) -> None:
    """Fresh schema (drop + create, so model changes never meet a stale table), empty Redis
    test DB."""
    engine = make_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    await redis.aclose()


class FakeRuntime:
    """In-memory SandboxRuntime: the worker's view of Docker, minus Docker."""

    def __init__(self) -> None:
        self.containers: dict[uuid.UUID, datetime] = {}
        self.launch_error: Exception | None = None
        self.remove_error: Exception | None = None
        self.list_error: Exception | None = None
        self.during_launch: Callable[[uuid.UUID], Awaitable[None]] | None = None

    async def launch(self, sandbox_id: uuid.UUID, expires_at: datetime) -> str:
        if self.launch_error:
            raise self.launch_error
        self.containers[sandbox_id] = expires_at
        if self.during_launch:
            await self.during_launch(sandbox_id)
        return "http://localhost:49153"

    async def remove(self, sandbox_id: uuid.UUID) -> bool:
        if self.remove_error:
            raise self.remove_error
        return self.containers.pop(sandbox_id, None) is not None

    async def list_sandboxes(self) -> list[RuntimeSandbox]:
        if self.list_error:
            raise self.list_error
        return [RuntimeSandbox(sid, exp) for sid, exp in self.containers.items()]


def metric(client: TestClient, name: str, **labels: str) -> float | None:
    """One sample from /metrics, matched on exact labels (exposition order doesn't matter)."""
    for family in text_string_to_metric_families(client.get("/metrics").text):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None
