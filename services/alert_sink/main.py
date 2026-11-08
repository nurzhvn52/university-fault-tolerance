"""Webhook receiver of Alertmanager: records when each notification arrived.

Stands in for the pager or chat of a team; the experiments read its log to measure the time
from a fault to the notification.
"""

import logging
import time
from collections import deque

from fastapi import APIRouter, FastAPI, Request

from common.app import create_service_app
from common.config import Settings, get_settings

logger = logging.getLogger("alert_sink")
router = APIRouter(tags=["alerts"])


@router.post("/webhook")
async def webhook(payload: dict, request: Request) -> dict:
    received = time.time()
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        entry = {
            "alertname": labels.get("alertname"),
            "status": alert.get("status"),
            "severity": labels.get("severity"),
            "labels": labels,
            "starts_at": alert.get("startsAt"),
            "ends_at": alert.get("endsAt"),
            "received_at": received,
        }
        request.app.state.received.append(entry)
        logger.warning("alert_notification", extra=entry)
    return {"accepted": len(payload.get("alerts", []))}


@router.get("/received")
async def received(request: Request) -> list:
    return list(request.app.state.received)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    async def init(app: FastAPI) -> None:
        app.state.received = deque(maxlen=2000)

    return create_service_app(
        settings, title="Alert sink", routers=[router], use_db=False, on_startup=init
    )


app = create_app()
