"""Timetable API: published timetable, slots of sections and generation jobs."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from common.db import SessionDep
from timetable import checkpointed
from timetable.jobs import start_generation
from timetable.models import GenerationJob, Room, Timeslot, TimetableEntry

router = APIRouter(prefix="/api/timetable", tags=["timetable"])

MAX_SECTION_IDS = 50


class EntryOut(BaseModel):
    section_id: int
    room: str
    timeslot_id: int
    day: str
    starts_at: str
    ends_at: str


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    term: str
    status: str
    sections_total: int
    sections_done: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


def _entries_query(term: str):
    return (
        select(
            TimetableEntry.section_id,
            Room.code.label("room"),
            Timeslot.id.label("timeslot_id"),
            Timeslot.day,
            Timeslot.starts_at,
            Timeslot.ends_at,
        )
        .join(Room, Room.id == TimetableEntry.room_id)
        .join(Timeslot, Timeslot.id == TimetableEntry.timeslot_id)
        .where(TimetableEntry.term == term)
        .order_by(TimetableEntry.section_id)
    )


@router.get("", response_model=list[EntryOut])
async def get_timetable(term: str, session: SessionDep) -> list[EntryOut]:
    rows = await session.execute(_entries_query(term))
    return [EntryOut(**row._mapping) for row in rows]


@router.get("/sections", response_model=list[EntryOut])
async def get_section_slots(term: str, ids: str, session: SessionDep) -> list[EntryOut]:
    try:
        section_ids = [int(part) for part in ids.split(",") if part]
    except ValueError as exc:
        raise HTTPException(422, "ids must be comma separated integers") from exc
    if not section_ids or len(section_ids) > MAX_SECTION_IDS:
        raise HTTPException(422, f"pass 1 to {MAX_SECTION_IDS} section ids")
    rows = await session.execute(
        _entries_query(term).where(TimetableEntry.section_id.in_(section_ids))
    )
    return [EntryOut(**row._mapping) for row in rows]


@router.post("/generate", response_model=JobOut, status_code=202)
async def generate(term: str, request: Request, session: SessionDep) -> GenerationJob:
    running = await session.scalar(
        select(GenerationJob.id).where(
            GenerationJob.term == term, GenerationJob.status == "RUNNING"
        )
    )
    ft = request.app.state.settings.ft
    if running is not None:
        if ft:
            # Idempotent in FT mode: the job already running (or resumed) is the answer.
            return await session.get(GenerationJob, running)
        raise HTTPException(409, "generation_in_progress")
    job = GenerationJob(term=term, status="RUNNING")
    session.add(job)
    await session.commit()
    if ft:
        checkpointed.start(request.app, job.id, resumed=False)
    else:
        start_generation(request.app, job.id, term)
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: int, request: Request, session: SessionDep) -> JobOut:
    job = await session.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(404, "job_not_found")
    out = JobOut.model_validate(job)
    if job.status == "RUNNING":
        out.sections_done = request.app.state.job_progress.get(job_id, out.sections_done)
    return out
