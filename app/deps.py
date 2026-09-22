from collections.abc import AsyncIterator
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_queue(request: Request) -> ArqRedis:
    return request.app.state.queue


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[ArqRedis, Depends(get_queue)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
