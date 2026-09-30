"""Database engine, sessions and the constraint naming convention used by all schemas.

Every service owns one PostgreSQL schema and never reads tables of another service.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData
from sqlalchemy import exc as sa_exc
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
    if not settings.ft:
        # Baseline: plain connection pool without pre-ping, connect or statement timeouts.
        return create_async_engine(
            settings.database_url, pool_size=settings.db_pool_size, max_overflow=5
        )
    # Fault-tolerant: a pooled connection is checked before use (it may belong to a database
    # that restarted or failed over), and connecting, querying and waiting for a free
    # connection all have a bounded time.
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        pool_timeout=settings.db_pool_timeout_s,
        pool_recycle=300,
        connect_args={
            "timeout": settings.db_connect_timeout_s,
            "command_timeout": settings.db_command_timeout_s,
        },
    )


# SQLSTATE of "the server cannot take the connection": class 08 (connection exception) and
# 57P01-57P03 (shutting down, crashed, starting up).
_UNAVAILABLE_STATES = ("08", "57P01", "57P02", "57P03")


def is_db_unavailable(exc: BaseException) -> bool:
    """The database cannot be reached or did not answer in time (as opposed to a bug or a
    constraint violation)."""
    if isinstance(exc, OSError | TimeoutError | sa_exc.TimeoutError | sa_exc.InterfaceError):
        return True
    if isinstance(exc, sa_exc.OperationalError):
        return True
    if not isinstance(exc, sa_exc.DBAPIError):
        return False
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None) or ""
    return exc.connection_invalidated or state.startswith(_UNAVAILABLE_STATES)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
