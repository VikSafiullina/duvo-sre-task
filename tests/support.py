from redis.asyncio import Redis
from sqlalchemy import text

from app.config import Settings
from app.db import create_schema, make_engine


async def reset_state(settings: Settings) -> None:
    """Fresh schema, empty tables, empty Redis test DB."""
    engine = make_engine(settings)
    await create_schema(engine)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sandboxes"))
    await engine.dispose()
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    await redis.aclose()
