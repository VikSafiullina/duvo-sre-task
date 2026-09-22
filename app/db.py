"""Engine/session factories shared by the API and the worker."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.models import Base


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        pool_timeout=settings.db_timeout_s,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_timeout_s, "command_timeout": settings.db_timeout_s},
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    """Scaffold shortcut: create tables at API startup. Production would run Alembic
    migrations as a separate release step (multiple replicas racing create_all is unsafe)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
