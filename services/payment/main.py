import asyncio
import contextlib

from fastapi import FastAPI

from common.app import create_service_app
from common.config import Settings, get_settings
from payment import journal
from payment.api import router


async def start_recovery(app: FastAPI) -> None:
    if app.state.settings.ft:
        app.state.bank = journal.bank_dependency(app.state.settings)
        app.state.recovery = asyncio.create_task(journal.recovery_loop(app))


async def stop_recovery(app: FastAPI) -> None:
    task = getattr(app.state, "recovery", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app(settings: Settings | None = None) -> FastAPI:
    return create_service_app(
        settings or get_settings(),
        title="Payment Service",
        routers=[router],
        on_startup=start_recovery,
        on_shutdown=stop_recovery,
    )


app = create_app()
