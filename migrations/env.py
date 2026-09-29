"""Alembic environment: one migration history for all service schemas."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings
from payment.models import Base as PaymentBase
from records.models import Base as RecordsBase
from student.models import Base as StudentBase
from timetable.models import Base as TimetableBase

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = [
    StudentBase.metadata,
    PaymentBase.metadata,
    RecordsBase.metadata,
    TimetableBase.metadata,
]
SCHEMAS = {metadata.schema for metadata in target_metadata}


def include_name(name, type_, parent_names) -> bool:
    if type_ == "schema":
        return name in SCHEMAS
    return True


def configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="public",
        compare_type=True,
        **kwargs,
    )


def run_offline() -> None:
    configure(url=get_settings().database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection) -> None:
    configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(get_settings().database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(run_sync_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
