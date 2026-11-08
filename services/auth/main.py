from fastapi import FastAPI

from auth.api import router
from common.app import create_service_app
from common.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    return create_service_app(settings or get_settings(), title="Auth Service", routers=[router])


app = create_app()
