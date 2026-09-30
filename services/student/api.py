"""Student API: students, course catalogue, sections and course registration."""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from common.db import SessionDep
from student import registration as ft_registration
from student.clients import PaymentClient, TimetableClient
from student.models import Course, Registration, Section, Student

logger = logging.getLogger("student.api")
router = APIRouter(prefix="/api", tags=["student"])


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    student_no: str
    full_name: str
    program: str
    year: int


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    title: str
    credits: int


class SectionOut(BaseModel):
    id: int
    course_id: int
    course_code: str
    title: str
    term: str
    capacity: int
    enrolled: int


class RegistrationIn(BaseModel):
    student_id: int = Field(gt=0)
    section_id: int = Field(gt=0)


class RegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    section_id: int
    status: str
    created_at: datetime


def _sections_query():
    return select(
        Section.id,
        Section.course_id,
        Course.code.label("course_code"),
        Course.title,
        Section.term,
        Section.capacity,
        Section.enrolled,
    ).join(Course, Course.id == Section.course_id)


@router.get("/students/{student_id}", response_model=StudentOut)
async def get_student(student_id: int, session: SessionDep) -> Student:
    student = await session.get(Student, student_id)
    if student is None:
        raise HTTPException(404, "student_not_found")
    return student


@router.get("/students/{student_id}/registrations", response_model=list[RegistrationOut])
async def list_registrations(student_id: int, session: SessionDep) -> list[Registration]:
    registrations = await session.scalars(
        select(Registration).where(Registration.student_id == student_id).order_by(Registration.id)
    )
    return list(registrations)


@router.get("/courses", response_model=list[CourseOut])
async def list_courses(session: SessionDep) -> list[Course]:
    return list(await session.scalars(select(Course).order_by(Course.code)))


@router.get("/sections", response_model=list[SectionOut])
async def list_sections(session: SessionDep, term: str | None = None) -> list[SectionOut]:
    query = _sections_query().order_by(Section.id)
    if term is not None:
        query = query.where(Section.term == term)
    rows = await session.execute(query)
    return [SectionOut(**row._mapping) for row in rows]


@router.get("/sections/{section_id}", response_model=SectionOut)
async def get_section(section_id: int, session: SessionDep) -> SectionOut:
    row = (await session.execute(_sections_query().where(Section.id == section_id))).first()
    if row is None:
        raise HTTPException(404, "section_not_found")
    return SectionOut(**row._mapping)


@router.post("/registrations", response_model=RegistrationOut, status_code=201)
async def register(body: RegistrationIn, request: Request, session: SessionDep):
    if request.app.state.settings.ft:
        # 201 confirmed, 202 accepted while tuition could not be checked yet.
        status, registration = await ft_registration.register(
            request.app, session, body.student_id, body.section_id
        )
        return JSONResponse(
            status_code=status,
            content=RegistrationOut.model_validate(registration).model_dump(mode="json"),
        )

    student = await session.get(Student, body.student_id)
    if student is None:
        raise HTTPException(404, "student_not_found")
    section = await session.get(Section, body.section_id)
    if section is None:
        raise HTTPException(404, "section_not_found")

    settings = request.app.state.settings
    http = request.app.state.http

    tuition = await PaymentClient(http, settings.payment_url).tuition_status(
        student.id, section.term
    )
    if not tuition["paid"]:
        raise HTTPException(402, "tuition_unpaid")

    taken = list(
        await session.scalars(
            select(Registration.section_id).where(Registration.student_id == student.id)
        )
    )
    if section.id in taken:
        raise HTTPException(409, "already_registered")
    slots = await TimetableClient(http, settings.timetable_url).slots(
        section.term, [section.id, *taken]
    )
    slot_of = {slot["section_id"]: slot["timeslot_id"] for slot in slots}
    if section.id not in slot_of:
        raise HTTPException(409, "section_not_scheduled")
    if any(slot_of.get(other) == slot_of[section.id] for other in taken):
        raise HTTPException(409, "schedule_conflict")

    # Baseline: read-modify-write of the seat counter. Two concurrent requests can read the
    # same value and both succeed, so a section can end up over capacity.
    if section.enrolled >= section.capacity:
        raise HTTPException(409, "section_full")
    section.enrolled += 1
    registration = Registration(student_id=student.id, section_id=section.id, status="CONFIRMED")
    session.add(registration)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(409, "already_registered") from exc
    logger.debug(
        "registration_confirmed",
        extra={"registration_id": registration.id, "section_id": section.id},
    )
    return registration


@router.delete("/registrations/{registration_id}", status_code=204)
async def cancel_registration(
    registration_id: int, request: Request, session: SessionDep
) -> Response:
    if request.app.state.settings.ft:
        await ft_registration.cancel(session, registration_id)
        return Response(status_code=204)

    registration = await session.get(Registration, registration_id)
    if registration is None:
        raise HTTPException(404, "registration_not_found")
    section = await session.get(Section, registration.section_id)
    section.enrolled -= 1
    await session.delete(registration)
    await session.commit()
    return Response(status_code=204)
