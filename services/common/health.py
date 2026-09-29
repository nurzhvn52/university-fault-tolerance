"""Liveness and readiness endpoints, the same in every service."""

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
    engine = request.app.state.engine
    if engine is not None:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse(
                status_code=503, content={"status": "unavailable", "reason": type(exc).__name__}
            )
    return {"status": "ok"}
