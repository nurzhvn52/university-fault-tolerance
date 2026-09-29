"""Background timetable generation jobs."""

import asyncio
import logging

from fastapi import FastAPI
from sqlalchemy import delete, func, select, update

from common.chaos import fault_point
from timetable.models import GenerationJob, Room, Timeslot, TimetableEntry
from timetable.scheduler import RoomInfo, Scheduler, SectionDemand, order_sections

logger = logging.getLogger("timetable.jobs")


class SchedulingError(Exception):
    pass


def start_generation(app: FastAPI, job_id: int, term: str) -> None:
    task = asyncio.create_task(run_generation(app, job_id, term))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)


async def fetch_sections(app: FastAPI, term: str) -> list[SectionDemand]:
    settings = app.state.settings
    response = await app.state.http.get(
        f"{settings.student_url.rstrip('/')}/api/sections", params={"term": term}
    )
    response.raise_for_status()
    return [SectionDemand(item["id"], item["capacity"]) for item in response.json()]


async def run_generation(app: FastAPI, job_id: int, term: str) -> None:
    sessionmaker = app.state.sessionmaker
    progress = app.state.job_progress
    try:
        sections = order_sections(await fetch_sections(app, term))
        async with sessionmaker() as session:
            rooms = list(await session.scalars(select(Room).order_by(Room.id)))
            timeslot_ids = list(await session.scalars(select(Timeslot.id).order_by(Timeslot.id)))
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(sections_total=len(sections))
            )
            await session.commit()

        scheduler = Scheduler([RoomInfo(room.id, room.capacity) for room in rooms], timeslot_ids)
        placements: dict[int, tuple[int, int]] = {}
        for section in sections:
            # Emulates the cost of a real optimisation step.
            await asyncio.sleep(app.state.settings.timetable_step_delay_s)
            placement = scheduler.place(section)
            if placement is None:
                raise SchedulingError(f"no free room for section {section.section_id}")
            placements[section.section_id] = placement
            # Baseline: progress lives only in memory, nothing is saved until the end.
            progress[job_id] = len(placements)
            await fault_point("timetable.section_placed")

        async with sessionmaker() as session:
            await session.execute(delete(TimetableEntry).where(TimetableEntry.term == term))
            session.add_all(
                TimetableEntry(
                    term=term,
                    section_id=section_id,
                    room_id=room_id,
                    timeslot_id=timeslot_id,
                    job_id=job_id,
                )
                for section_id, (room_id, timeslot_id) in placements.items()
            )
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(status="COMPLETED", sections_done=len(placements), finished_at=func.now())
            )
            await session.commit()
        logger.info("timetable_generated", extra={"job_id": job_id, "sections": len(placements)})
    except Exception as exc:
        logger.exception("timetable_generation_failed", extra={"job_id": job_id})
        async with sessionmaker() as session:
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(status="FAILED", error=repr(exc), finished_at=func.now())
            )
            await session.commit()
    finally:
        progress.pop(job_id, None)
