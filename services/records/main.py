from fastapi import FastAPI

from common.app import create_service_app
from common.config import Settings, get_settings
from records.api import router
from records.storage import LocalStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    async def open_storage(app: FastAPI) -> None:
        app.state.storage = LocalStorage(settings.transcript_dir)

    return create_service_app(
        settings, title="Academic Records Service", routers=[router], on_startup=open_storage
    )


app = create_app()
