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
from common.audit import AuditLog, NullAudit
from common.config import Settings
from common.context import REQUEST_ID_RE, current_request_id, new_request_id, request_id_var
from common.db import create_engine, is_db_unavailable
from common.health import router as health_router
from common.logs import configure_logging
from common.ratelimit import RateLimiter
from common.resilience import Bulkhead, DependencyUnavailable, RateLimitedLog
from common.security import ServiceTokens, client_address, load_bank_api_key, validate_config
from common.telemetry import BUILD_INFO, IN_FLIGHT, LATENCY, REJECTED, REQUESTS, SHED

logger = logging.getLogger("common.app")

Hook = Callable[[FastAPI], Awaitable[None]]


async def forward_request_id(request: httpx.Request) -> None:
    """Outgoing calls carry the id of the request being served, so that one user request can
    be followed through every service it touches."""
    request_id = current_request_id()
    if request_id and "x-request-id" not in request.headers:
        request.headers["X-Request-ID"] = request_id


def _reject_oversized(request: Request, settings: Settings) -> tuple[int, str] | None:
    """Writes must declare a body size within the limit (a missing or huge size is refused
    before the body is read)."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return None
    length = request.headers.get("content-length")
    if length is None:
        return 411, "length_required"
    try:
        size = int(length)
    except ValueError:
        return 400, "bad_content_length"
    if size > settings.max_body_bytes:
        return 413, "body_too_large"
    return None


def make_credentials_hook(settings: Settings, secrets: list[str]):
    """Outgoing calls to other services carry a short-lived service token; calls to the bank
    carry its API key instead (the bank must never see an internal token)."""
    tokens = ServiceTokens(settings, secrets) if secrets else None
    bank_key = load_bank_api_key(settings)
    bank = httpx.URL(settings.bank_url)
    bank_origin = (bank.host, bank.port)

    async def hook(request: httpx.Request) -> None:
        if (request.url.host, request.url.port) == bank_origin:
            if bank_key:
                request.headers["X-Api-Key"] = bank_key
        elif tokens is not None and "authorization" not in request.headers:
            request.headers["Authorization"] = f"Bearer {tokens.get()}"

    return hook


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
        app.state.jwt_secrets = validate_config(settings)
        if use_db:
            app.state.engine = create_engine(settings)
            app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
        if settings.security:
            app.state.limiter = RateLimiter(settings.rate_limit_rps, settings.rate_limit_burst)
            app.state.audit = AuditLog(
                settings.service_name, app.state.engine, queue_size=settings.audit_queue
            )
            app.state.audit.start()
        else:
            app.state.audit = NullAudit()
        # Baseline: calls to other services have no timeout. In FT mode every call also gets
        # its own deadline from the Dependency wrapper; this is only the outer limit.
        hooks = []
        if settings.tracing:
            hooks.append(forward_request_id)
        if settings.security:
            hooks.append(make_credentials_hook(settings, app.state.jwt_secrets))
        hooks = {"request": hooks} if hooks else {}
        app.state.http = httpx.AsyncClient(timeout=5.0 if settings.ft else None, event_hooks=hooks)
        BUILD_INFO.labels(settings.service_name, settings.version, settings.ft_mode).set(1)
        if on_startup is not None:
            await on_startup(app)
        logger.info("service_started", extra={"mode": settings.ft_mode})
        try:
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown(app)
            await app.state.http.aclose()
            await app.state.audit.stop()
            if app.state.engine is not None:
                await app.state.engine.dispose()
            logger.info("service_stopped")

    app = FastAPI(title=title, lifespan=lifespan)
    instance = socket.gethostname()
    # FT: load shedding. An instance works on at most max_in_flight requests; the rest get an
    # immediate 503 instead of queueing until everything times out.
    inbound = Bulkhead("inbound", settings.max_in_flight) if settings.ft else None
    rate_log = RateLimitedLog()
    fail_log = RateLimitedLog(1.0)

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
        request_id = None
        id_token = None
        if settings.tracing:
            request_id = request.headers.get("x-request-id", "")
            if not REQUEST_ID_RE.match(request_id):
                request_id = new_request_id()
            id_token = request_id_var.set(request_id)
        rejection = _reject_oversized(request, settings) if settings.security and api else None
        if rejection is not None:
            status, reason = rejection
            REJECTED.labels(service, reason).inc()
            request.app.state.audit.record(
                "request.rejected",
                "denied",
                actor=client_address(request),
                resource=request.url.path,
                detail={"reason": reason, "status": status, "method": request.method},
                dedup=(reason, client_address(request)),
            )
            response = JSONResponse(status_code=status, content={"error": reason})
        elif guarded and not inbound.try_acquire():
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
        duration = time.perf_counter() - started
        if api:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            REQUESTS.labels(service, route, request.method, response.status_code).inc()
            LATENCY.labels(service, route).observe(duration)
            if settings.access_log:
                logger.info(
                    "request",
                    extra={
                        "method": request.method,
                        "route": route,
                        "status": response.status_code,
                        "duration_ms": round(duration * 1000),
                    },
                )
            if (
                settings.tracing
                and response.status_code >= 500
                and fail_log.should_log(f"{route}:{response.status_code}")
            ):
                logger.warning(
                    "request_failed",
                    extra={
                        "method": request.method,
                        "route": route,
                        "status": response.status_code,
                        "duration_ms": round(duration * 1000),
                    },
                )
        # Shows which replica served the request.
        response.headers["X-Instance"] = instance
        if request_id is not None:
            response.headers["X-Request-ID"] = request_id
            request_id_var.reset(id_token)
        return response

    app.include_router(health_router)
    app.mount("/metrics", make_asgi_app())
    if settings.chaos_enabled:
        app.include_router(chaos.router)
    for router in routers:
        app.include_router(router)
    return app
