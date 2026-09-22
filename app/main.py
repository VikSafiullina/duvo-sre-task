"""FastAPI application factory."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import create_schema, make_engine, make_sessionmaker
from app.observability import observe_requests, setup_logging
from app.queue import redis_settings
from app.routes import health, metrics, sandboxes

log = logging.getLogger("app")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.settings = settings
        app.state.engine = make_engine(settings)
        app.state.sessionmaker = make_sessionmaker(app.state.engine)
        app.state.queue = await create_pool(redis_settings(settings))
        await create_schema(app.state.engine)
        log.info("api started", extra={"environment": settings.environment})
        yield
        await app.state.queue.aclose()
        await app.state.engine.dispose()
        log.info("api stopped")

    app = FastAPI(title=settings.service_name, lifespan=lifespan)
    app.middleware("http")(observe_requests)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(sandboxes.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app


app = create_app()
