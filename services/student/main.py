from fastapi import FastAPI

from common.app import create_service_app
from common.config import Settings, get_settings
from student.api import router


def create_app(settings: Settings | None = None) -> FastAPI:
    return create_service_app(settings or get_settings(), title="Student Service", routers=[router])


app = create_app()
