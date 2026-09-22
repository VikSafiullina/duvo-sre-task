import asyncio
from collections.abc import Awaitable

from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Deliberately no dependency checks — a DB outage
    must not make the orchestrator restart every replica (restart storm)."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness: can this replica serve traffic right now? Checks DB + Redis with a timeout."""
    state = request.app.state
    settings = state.settings
    checks = {
        "database": await _check(_ping_db(state.engine), settings.db_timeout_s),
        "redis": await _check(state.queue.ping(), settings.redis_timeout_s),
    }
    ok = all(v == "ok" for v in checks.values())
    response.status_code = 200 if ok else 503
    return {"status": "ok" if ok else "degraded", "checks": checks}


async def _check(probe: Awaitable[object], timeout_s: float) -> str:
    try:
        async with asyncio.timeout(timeout_s):
            await probe
    except Exception as exc:
        return f"error: {type(exc).__name__}"
    return "ok"


async def _ping_db(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
