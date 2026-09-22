from redis.asyncio import Redis

from app.config import Settings
from app.db import create_schema, make_engine
from app.models import Base


async def reset_state(settings: Settings) -> None:
    """Fresh schema (drop + create, so new columns land without migrations), empty Redis
    test DB."""
    engine = make_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(engine)
    await engine.dispose()
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    await redis.aclose()
