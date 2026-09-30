"""Waits until the database accepts connections (used before the migrations in the FT stack,
where Patroni elects the primary a few seconds after the start)."""

import asyncio
import sys
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings

TIMEOUT_S = 180


async def main() -> int:
    engine = create_async_engine(get_settings().database_url, connect_args={"timeout": 2})
    deadline = time.monotonic() + TIMEOUT_S
    try:
        while True:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                print("database is ready", flush=True)
                return 0
            except Exception as exc:
                if time.monotonic() > deadline:
                    print(f"database not ready after {TIMEOUT_S} s: {exc!r}", file=sys.stderr)
                    return 1
                await asyncio.sleep(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
