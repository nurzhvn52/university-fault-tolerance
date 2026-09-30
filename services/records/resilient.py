"""Fault-tolerant parts of the records service (FT mode).

- Grade import is one transaction with an idempotent upsert: an interrupted import leaves
  nothing behind (rollback), and sending the same batch again gives the same result.
- Transcripts degrade gracefully: every transcript computed from the database is also kept
  in transcript storage, and a background worker refreshes all of them. When the database is
  unavailable the stored copy is served, marked as stale.
"""

import asyncio
import itertools
import json
import logging

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.chaos import fault_point
from common.resilience import RateLimitedLog
from records.gpa import GRADE_POINTS
from records.models import Grade
from records.schemas import GradeIn, TranscriptOut, build_transcript

logger = logging.getLogger("records.resilient")
_rate_log = RateLimitedLog()


async def import_grades(session: AsyncSession, items: list[GradeIn]) -> int:
    for item in items:
        values = {**item.model_dump(), "points": GRADE_POINTS[item.letter]}
        await session.execute(
            insert(Grade)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["student_id", "course_code", "term"],
                set_={
                    "letter": values["letter"],
                    "points": values["points"],
                    "credits": values["credits"],
                },
            )
        )
        # A crash here rolls the whole batch back: nothing has been committed yet.
        await fault_point("records.grade_imported")
    await session.commit()
    return len(items)


def _cache_key(student_id: int) -> str:
    return f"cache/{student_id}.json"


def store(app: FastAPI, transcript: TranscriptOut) -> None:
    data = json.dumps(transcript.model_dump(mode="json"), sort_keys=True).encode()
    app.state.storage.write(_cache_key(transcript.student_id), data)


def cached(app: FastAPI, student_id: int) -> TranscriptOut | None:
    try:
        data = app.state.storage.read(_cache_key(student_id))
    except OSError:
        return None
    if _rate_log.should_log("fallback"):
        logger.warning("fallback_used", extra={"dependency": "db", "fallback": "transcript_cache"})
    return TranscriptOut.model_validate({**json.loads(data), "stale": True})


async def warm_cache(app: FastAPI) -> int:
    async with app.state.sessionmaker() as session:
        grades = list(
            await session.scalars(select(Grade).order_by(Grade.student_id, Grade.term, Grade.id))
        )
    count = 0
    for student_id, group in itertools.groupby(grades, key=lambda grade: grade.student_id):
        store(app, build_transcript(student_id, group, voting=True))
        count += 1
    return count


async def cache_loop(app: FastAPI) -> None:
    settings = app.state.settings
    while True:
        try:
            count = await warm_cache(app)
            logger.info("transcript_cache_refreshed", extra={"students": count})
            delay = settings.transcript_cache_refresh_s
        except Exception as exc:
            if _rate_log.should_log("warm"):
                logger.warning("transcript_cache_failed", extra={"error": type(exc).__name__})
            delay = settings.worker_interval_s
        await asyncio.sleep(delay)
