from fastapi import FastAPI

from common.app import create_service_app
from common.config import Settings, get_settings
from timetable.api import router


async def init_jobs(app: FastAPI) -> None:
    app.state.tasks = set()
    app.state.job_progress = {}


def create_app(settings: Settings | None = None) -> FastAPI:
    return create_service_app(
        settings or get_settings(),
        title="Timetable Service",
        routers=[router],
        on_startup=init_jobs,
    )


app = create_app()
