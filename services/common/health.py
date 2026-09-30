"""Liveness and readiness endpoints, the same in every service."""

import asyncio
import socket

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "service": settings.service_name,
        "instance": socket.gethostname(),
        "mode": settings.ft_mode,
    }


@router.get("/ready")
async def ready(request: Request):
    """Ready to take traffic. A service that can degrade without the database (records
    serves cached transcripts) sets ``app.state.ready_without_db``."""
    engine = request.app.state.engine
    settings = request.app.state.settings
    if engine is not None and not getattr(request.app.state, "ready_without_db", False):
        try:
            async with asyncio.timeout(1.0 if settings.ft else None), engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse(
                status_code=503, content={"status": "unavailable", "reason": type(exc).__name__}
            )
    return {"status": "ok"}
