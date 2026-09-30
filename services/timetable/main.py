import asyncio
import contextlib

from fastapi import FastAPI

from common.app import create_service_app
from common.config import Settings, get_settings
from timetable import checkpointed
from timetable.api import router


async def init_jobs(app: FastAPI) -> None:
    app.state.tasks = set()
    app.state.job_progress = {}
    app.state.running_jobs = set()
    if app.state.settings.ft:
        app.state.deps = checkpointed.dependencies(app.state.settings)
        app.state.resume_task = asyncio.create_task(checkpointed.resume_loop(app))


async def stop_jobs(app: FastAPI) -> None:
    task = getattr(app.state, "resume_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app(settings: Settings | None = None) -> FastAPI:
    return create_service_app(
        settings or get_settings(),
        title="Timetable Service",
        routers=[router],
        on_startup=init_jobs,
        on_shutdown=stop_jobs,
    )


app = create_app()
