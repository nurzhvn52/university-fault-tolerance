import asyncio
import contextlib

from fastapi import FastAPI

from common.app import create_service_app
from common.config import Settings, get_settings
from records import resilient
from records.api import router
from records.storage import LocalStorage, MirroredStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    disks = [d for d in settings.transcript_dirs.split(",") if d]

    async def start(app: FastAPI) -> None:
        app.state.tasks = []
        if len(disks) > 1:
            app.state.storage = MirroredStorage(disks)
            app.state.tasks.append(asyncio.create_task(resilient.scrub_loop(app)))
        else:
            app.state.storage = LocalStorage(settings.transcript_dir)
        if settings.ft:
            # Cached transcripts are served when the database is down, so stay routable.
            app.state.ready_without_db = True
            app.state.tasks.append(asyncio.create_task(resilient.cache_loop(app)))

    async def stop(app: FastAPI) -> None:
        for task in app.state.tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return create_service_app(
        settings,
        title="Academic Records Service",
        routers=[router],
        on_startup=start,
        on_shutdown=stop,
    )


app = create_app()
