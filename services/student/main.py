import asyncio
import contextlib

from fastapi import FastAPI

from common.app import create_service_app
from common.config import Settings, get_settings
from student import registration
from student.api import router


async def start_background(app: FastAPI) -> None:
    if app.state.settings.ft:
        app.state.deps = registration.dependencies(app.state.settings)
        app.state.timetable_cache = {}
        app.state.background = asyncio.create_task(registration.background_loop(app))


async def stop_background(app: FastAPI) -> None:
    task = getattr(app.state, "background", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app(settings: Settings | None = None) -> FastAPI:
    return create_service_app(
        settings or get_settings(),
        title="Student Service",
        routers=[router],
        on_startup=start_background,
        on_shutdown=stop_background,
    )


app = create_app()
