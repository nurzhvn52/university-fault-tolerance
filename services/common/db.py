"""Database engine, sessions and the constraint naming convention used by all schemas.

Every service owns one PostgreSQL schema and never reads tables of another service.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from common.config import Settings

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def schema_metadata(schema: str) -> MetaData:
    return MetaData(schema=schema, naming_convention=NAMING_CONVENTION)


def create_engine(settings: Settings) -> AsyncEngine:
    # Baseline: plain connection pool without pre-ping, connect or statement timeouts.
    return create_async_engine(
        settings.database_url, pool_size=settings.db_pool_size, max_overflow=5
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
