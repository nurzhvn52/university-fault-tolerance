"""FastAPI application factory shared by the services."""

import logging
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from sqlalchemy.ext.asyncio import async_sessionmaker

from common import chaos
from common.config import Settings
from common.db import create_engine, is_db_unavailable
from common.health import router as health_router
from common.logs import configure_logging
from common.resilience import Bulkhead, DependencyUnavailable, RateLimitedLog
from common.telemetry import IN_FLIGHT, LATENCY, REQUESTS, SHED

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
        # Baseline: calls to other services have no timeout. In FT mode every call also gets
        # its own deadline from the Dependency wrapper; this is only the outer limit.
        app.state.http = httpx.AsyncClient(timeout=5.0 if settings.ft else None)
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
    # FT: load shedding. An instance works on at most max_in_flight requests; the rest get an
    # immediate 503 instead of queueing until everything times out.
    inbound = Bulkhead("inbound", settings.max_in_flight) if settings.ft else None
    rate_log = RateLimitedLog()

    def error_response(request: Request, exc: Exception) -> JSONResponse:
        if settings.ft and isinstance(exc, DependencyUnavailable):
            name = str(exc)
            if rate_log.should_log(f"dep:{name}"):
                logger.warning("dependency_unavailable", extra={"dependency": name})
            return JSONResponse(
                status_code=503,
                content={"error": "dependency_unavailable", "dependency": name},
                headers={"Retry-After": "1"},
            )
        if settings.ft and is_db_unavailable(exc):
            if rate_log.should_log("db"):
                logger.warning("db_unavailable", extra={"error": type(exc).__name__})
            return JSONResponse(
                status_code=503,
                content={"error": "database_unavailable"},
                headers={"Retry-After": "1"},
            )
        logger.exception(
            "unhandled_error", extra={"path": request.url.path, "error": type(exc).__name__}
        )
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    service = settings.service_name

    @app.middleware("http")
    async def instance_header_and_errors(request: Request, call_next):
        api = request.url.path.startswith("/api")
        guarded = inbound is not None and api
        started = time.perf_counter()
        if guarded and not inbound.try_acquire():
            if rate_log.should_log("shed"):
                logger.warning("load_shed", extra={"in_flight": inbound.in_use})
            SHED.labels(service).inc()
            response = JSONResponse(
                status_code=503, content={"error": "overloaded"}, headers={"Retry-After": "1"}
            )
        else:
            IN_FLIGHT.labels(service).inc()
            try:
                response = await call_next(request)
            except Exception as exc:
                response = error_response(request, exc)
            finally:
                IN_FLIGHT.labels(service).dec()
                if guarded:
                    inbound.release()
        if api:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            REQUESTS.labels(service, route, request.method, response.status_code).inc()
            LATENCY.labels(service, route).observe(time.perf_counter() - started)
        # Shows which replica served the request.
        response.headers["X-Instance"] = instance
        return response

    app.include_router(health_router)
    app.mount("/metrics", make_asgi_app())
    if settings.chaos_enabled:
        app.include_router(chaos.router)
    for router in routers:
        app.include_router(router)
    return app
