"""FastAPI application factory shared by the services."""

import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from common.config import Settings
from common.db import create_engine
from common.health import router as health_router
from common.logs import configure_logging

logger = logging.getLogger("common.app")

Hook = Callable[[FastAPI], Awaitable[None]]


def create_service_app(
    settings: Settings,
    *,
    title: str,
    routers: list[APIRouter],
    use_db: bool = True,
    on_startup: Hook | None = None,
    on_shutdown: Hook | None = None,
) -> FastAPI:
    configure_logging(settings.service_name, settings.node, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = None
        if use_db:
            app.state.engine = create_engine(settings)
            app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
        # Baseline: calls to other services have no timeout.
        app.state.http = httpx.AsyncClient(timeout=None)
        if on_startup is not None:
            await on_startup(app)
        logger.info("service_started", extra={"mode": settings.ft_mode})
        try:
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown(app)
            await app.state.http.aclose()
            if app.state.engine is not None:
                await app.state.engine.dispose()
            logger.info("service_stopped")

    app = FastAPI(title=title, lifespan=lifespan)
    instance = socket.gethostname()

    @app.middleware("http")
    async def instance_header_and_errors(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception(
                "unhandled_error", extra={"path": request.url.path, "error": type(exc).__name__}
            )
            response = JSONResponse(status_code=500, content={"error": "internal_error"})
        # Shows which replica served the request.
        response.headers["X-Instance"] = instance
        return response

    app.include_router(health_router)
    for router in routers:
        app.include_router(router)
    return app
