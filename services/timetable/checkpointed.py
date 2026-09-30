"""Fault-tolerant timetable generation (FT mode): checkpoints, leases, recovery block.

- Checkpointing: every few placed sections the placements so far are saved in the job row.
- Leases: a job belongs to the instance holding its lease (owner and expiry), renewed at
  every checkpoint. If the owner dies, the lease runs out and the job is resumed from the
  last checkpoint by another instance; an instance restarted under the same name takes its
  own job back at once.
- Recovery block: the result of the balanced primary algorithm must pass an acceptance test;
  if it does not (or the primary fails), the simple first-fit alternate is run and has to
  pass the test without the balance criterion.
- A job waiting on the database or the student service stays RUNNING and is resumed later
  instead of being marked FAILED.
"""

import asyncio
import logging
import socket
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy import delete, func, or_, select, update

from common.chaos import InjectedFault, fault_point
from common.db import is_db_unavailable
from common.resilience import Dependency, DependencyUnavailable, RateLimitedLog
from timetable.models import GenerationJob, Room, Timeslot, TimetableEntry
from timetable.scheduler import (
    RoomInfo,
    Scheduler,
    SectionDemand,
    acceptance_test,
    first_fit,
    order_sections,
)

logger = logging.getLogger("timetable.checkpointed")
_rate_log = RateLimitedLog()

LEASE_S = 15
NEW_JOB_GRACE_S = 5
INSTANCE = socket.gethostname()


class LeaseLost(Exception):
    """Another instance took the job over; this one stops working on it."""


def dependencies(settings) -> dict[str, Dependency]:
    return {"student": Dependency.from_settings("student", settings, 2.0)}


def start(app: FastAPI, job_id: int, *, resumed: bool) -> None:
    if job_id in app.state.running_jobs:
        return
    app.state.running_jobs.add(job_id)
    task = asyncio.create_task(run(app, job_id, resumed=resumed))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
    task.add_done_callback(lambda _: app.state.running_jobs.discard(job_id))


async def _claim(app: FastAPI, job_id: int):
    async with app.state.sessionmaker() as session:
        row = (
            await session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == "RUNNING",
                    or_(
                        GenerationJob.lease_owner.is_(None),
                        GenerationJob.lease_owner == INSTANCE,
                        GenerationJob.lease_until < func.now(),
                    ),
                )
                .values(lease_owner=INSTANCE, lease_until=func.now() + timedelta(seconds=LEASE_S))
                .returning(GenerationJob.term, GenerationJob.checkpoint)
                .execution_options(synchronize_session=False)
            )
        ).first()
        await session.commit()
    return row


async def _save_checkpoint(app: FastAPI, job_id: int, placements: dict) -> None:
    async with app.state.sessionmaker() as session:
        saved = await session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.lease_owner == INSTANCE)
            .values(
                checkpoint={str(sid): list(p) for sid, p in placements.items()},
                sections_done=len(placements),
                lease_until=func.now() + timedelta(seconds=LEASE_S),
            )
            .returning(GenerationJob.id)
            .execution_options(synchronize_session=False)
        )
        lost = saved.first() is None
        await session.commit()
    if lost:
        raise LeaseLost(job_id)


async def _fetch_sections(app: FastAPI, term: str) -> list[SectionDemand]:
    url = f"{app.state.settings.student_url.rstrip('/')}/api/sections"

    async def call() -> list[dict]:
        response = await app.state.http.get(url, params={"term": term})
        response.raise_for_status()
        return response.json()

    items = await app.state.deps["student"].call(call)
    return [SectionDemand(item["id"], item["capacity"]) for item in items]


async def _primary(app, job_id, sections, rooms, timeslot_ids, placements) -> dict | None:
    """Balanced greedy placement, continuing from the checkpointed placements."""
    settings = app.state.settings
    scheduler = Scheduler(rooms, timeslot_ids)
    for room_id, timeslot_id in placements.values():
        scheduler.reserve(room_id, timeslot_id)
    for section in sections:
        if section.section_id in placements:
            continue
        # Emulates the cost of a real optimisation step.
        await asyncio.sleep(settings.timetable_step_delay_s)
        placement = scheduler.place(section)
        if placement is None:
            return None
        placements[section.section_id] = placement
        app.state.job_progress[job_id] = len(placements)
        if len(placements) % settings.timetable_checkpoint_every == 0:
            await _save_checkpoint(app, job_id, placements)
        await fault_point("timetable.section_placed")
    try:
        # Lets an experiment make the primary version fail and exercise the alternate.
        await fault_point("timetable.primary_result")
    except InjectedFault:
        return None
    return placements


async def run(app: FastAPI, job_id: int, *, resumed: bool) -> None:
    claimed = await _claim(app, job_id)
    if claimed is None:
        return
    term = claimed.term
    placements = {int(sid): tuple(p) for sid, p in (claimed.checkpoint or {}).items()}
    if resumed:
        logger.info("job_resumed", extra={"job_id": job_id, "from_sections": len(placements)})
    try:
        sections = order_sections(await _fetch_sections(app, term))
        async with app.state.sessionmaker() as session:
            rooms = [RoomInfo(r.id, r.capacity) for r in await session.scalars(select(Room))]
            timeslot_ids = list(await session.scalars(select(Timeslot.id).order_by(Timeslot.id)))
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(sections_total=len(sections))
            )
            await session.commit()

        result = await _primary(app, job_id, sections, rooms, timeslot_ids, placements)
        problems = acceptance_test(result, sections, rooms, timeslot_ids, balanced=True)
        if problems:
            logger.warning("alternate_used", extra={"job_id": job_id, "problems": problems})
            result = first_fit(sections, rooms, timeslot_ids)
            problems = acceptance_test(result, sections, rooms, timeslot_ids, balanced=False)
            if problems:
                raise ValueError("; ".join(problems))
        await _publish(app, job_id, term, result)
        logger.info("timetable_generated", extra={"job_id": job_id, "sections": len(result)})
    except LeaseLost:
        logger.info("job_taken_over", extra={"job_id": job_id})
    except Exception as exc:
        if isinstance(exc, DependencyUnavailable) or is_db_unavailable(exc):
            # Stays RUNNING; the lease runs out and the job is resumed later.
            if _rate_log.should_log(f"paused:{job_id}"):
                logger.warning("job_paused", extra={"job_id": job_id, "error": type(exc).__name__})
            return
        logger.exception("timetable_generation_failed", extra={"job_id": job_id})
        async with app.state.sessionmaker() as session:
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(status="FAILED", error=repr(exc), finished_at=func.now())
            )
            await session.commit()
    finally:
        app.state.job_progress.pop(job_id, None)


async def _publish(app: FastAPI, job_id: int, term: str, placements: dict) -> None:
    """Replace the timetable of the term and complete the job in one transaction."""
    async with app.state.sessionmaker() as session:
        done = await session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.lease_owner == INSTANCE)
            .values(
                status="COMPLETED",
                sections_done=len(placements),
                finished_at=func.now(),
                lease_owner=None,
                lease_until=None,
            )
            .returning(GenerationJob.id)
            .execution_options(synchronize_session=False)
        )
        if done.first() is None:
            await session.rollback()
            raise LeaseLost(job_id)
        await session.execute(delete(TimetableEntry).where(TimetableEntry.term == term))
        session.add_all(
            TimetableEntry(
                term=term, section_id=sid, room_id=room_id, timeslot_id=slot, job_id=job_id
            )
            for sid, (room_id, slot) in placements.items()
        )
        await session.commit()


async def resume_loop(app: FastAPI) -> None:
    """Finds RUNNING jobs whose owner is gone (expired lease, or this instance before a
    restart) and resumes them from their checkpoint."""
    while True:
        try:
            async with app.state.sessionmaker() as session:
                orphaned = await session.scalars(
                    select(GenerationJob.id).where(
                        GenerationJob.status == "RUNNING",
                        or_(
                            GenerationJob.lease_until < func.now(),
                            GenerationJob.lease_owner == INSTANCE,
                            (GenerationJob.lease_owner.is_(None))
                            & (
                                GenerationJob.started_at
                                < func.now() - timedelta(seconds=NEW_JOB_GRACE_S)
                            ),
                        ),
                    )
                )
                job_ids = list(orphaned)
            for job_id in job_ids:
                start(app, job_id, resumed=True)
        except Exception as exc:
            if _rate_log.should_log("resume"):
                logger.warning("resume_scan_failed", extra={"error": type(exc).__name__})
        await asyncio.sleep(app.state.settings.worker_interval_s)
