"""Accounts for the auth service: staff and the first students (synthetic, shared lab password).

Run after the migrations: ``python -m tools.seed_auth``. Does nothing if users already exist.
Students without an account can still be given tokens by the lab (the token is the identity;
the account only matters for the password login).
"""

import asyncio
import logging
import os

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from auth.models import User
from auth.passwords import hash_password
from common.config import get_settings
from common.logs import configure_logging
from student.models import Student

logger = logging.getLogger("tools.seed_auth")
STUDENT_ACCOUNTS = 50


async def main() -> None:
    settings = get_settings()
    configure_logging("seed_auth", "job", settings.log_level)
    password = os.environ.get("UFT_SEED_PASSWORD", "synthetic-lab-password")
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        if await conn.scalar(select(func.count()).select_from(User)):
            logger.info("users_exist")
            return
        students = (
            await conn.execute(
                select(Student.id, Student.student_no).order_by(Student.id).limit(STUDENT_ACCOUNTS)
            )
        ).all()
        rows = [
            {"username": "registrar1", "role": "registrar", "student_id": None},
            {"username": "admin1", "role": "admin", "student_id": None},
            *({"username": no, "role": "student", "student_id": sid} for sid, no in students),
        ]
        hashes = await asyncio.gather(*(asyncio.to_thread(hash_password, password) for _ in rows))
        for row, stored in zip(rows, hashes, strict=True):
            row["password_hash"] = stored
        await conn.execute(insert(User), rows)
    await engine.dispose()
    logger.info("users_created", extra={"count": len(rows)})


if __name__ == "__main__":
    asyncio.run(main())
