import os

# Isolated test targets; CI overrides via env. Must run before app modules read settings.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.support import reset_state


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI, settings: Settings) -> Iterator[TestClient]:
    asyncio.run(reset_state(settings))
    with TestClient(app) as c:
        yield c
