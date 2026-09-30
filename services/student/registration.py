"""Fault-tolerant course registration (FT mode).

Differences from the baseline:
- the database connection is given back before other services are called, so a slow
  payment or timetable service cannot hold all pooled connections (bulkhead);
- calls to payment and timetable have timeouts, retries and circuit breakers;
- graceful degradation: if the payment service is unavailable the registration is accepted
  as PENDING_VERIFICATION and checked later by a background worker; if the timetable service
  is unavailable, conflicts are checked against a cached copy of the timetable;
- the seat is reserved with one conditional UPDATE (enrolled < capacity), so concurrent
  registrations cannot overbook a section or lose counter updates.
"""

import asyncio
import logging

from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from common.resilience import Dependency, DependencyUnavailable, RateLimitedLog
from student.clients import PaymentClient, TimetableClient
from student.models import Registration, Section, Student

logger = logging.getLogger("student.registration")
_rate_log = RateLimitedLog()

CONFIRMED = "CONFIRMED"
PENDING = "PENDING_VERIFICATION"
CACHE_REFRESH_S = 30


def dependencies(settings) -> dict[str, Dependency]:
    return {
        "payment": Dependency.from_settings("payment", settings, settings.payment_timeout_s, 2),
        "timetable": Dependency.from_settings(
            "timetable", settings, settings.timetable_timeout_s, 2
        ),
    }


async def _tuition_paid(app: FastAPI, student_id: int, term: str) -> bool:
    client = PaymentClient(app.state.http, app.state.settings.payment_url)
    tuition = await app.state.deps["payment"].call(lambda: client.tuition_status(student_id, term))
    return tuition["paid"]


async def _slots(app: FastAPI, term: str, section_ids: list[int]) -> dict[int, int]:
    client = TimetableClient(app.state.http, app.state.settings.timetable_url)
    try:
        slots = await app.state.deps["timetable"].call(lambda: client.slots(term, section_ids))
    except DependencyUnavailable:
        cached = app.state.timetable_cache.get(term)
        if cached is None:
            raise
        if _rate_log.should_log("timetable_cache"):
            logger.warning("fallback_used", extra={"dependency": "timetable", "fallback": "cache"})
        return {sid: cached[sid] for sid in section_ids if sid in cached}
    return {slot["section_id"]: slot["timeslot_id"] for slot in slots}


async def register(
    app: FastAPI, session: AsyncSession, student_id: int, section_id: int
) -> tuple[int, Registration]:
    student = await session.get(Student, student_id)
    if student is None:
        raise HTTPException(404, "student_not_found")
    section = await session.get(Section, section_id)
    if section is None:
        raise HTTPException(404, "section_not_found")
    term = section.term
    taken = list(
        await session.scalars(
            select(Registration.section_id).where(Registration.student_id == student_id)
        )
    )
    if section_id in taken:
        raise HTTPException(409, "already_registered")
    # Give the connection back to the pool before waiting on other services.
    await session.commit()

    status = CONFIRMED
    try:
        if not await _tuition_paid(app, student_id, term):
            raise HTTPException(402, "tuition_unpaid")
    except DependencyUnavailable:
        status = PENDING
        if _rate_log.should_log("payment_fallback"):
            logger.warning("fallback_used", extra={"dependency": "payment", "fallback": "defer"})

    slot_of = await _slots(app, term, [section_id, *taken])
    if section_id not in slot_of:
        raise HTTPException(409, "section_not_scheduled")
    if any(slot_of.get(other) == slot_of[section_id] for other in taken):
        raise HTTPException(409, "schedule_conflict")

    reserved = await session.execute(
        update(Section)
        .where(Section.id == section_id, Section.enrolled < Section.capacity)
        .values(enrolled=Section.enrolled + 1)
        .returning(Section.id)
        .execution_options(synchronize_session=False)
    )
    if reserved.first() is None:
        await session.rollback()
        raise HTTPException(409, "section_full")
    registration = Registration(student_id=student_id, section_id=section_id, status=status)
    session.add(registration)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "already_registered") from exc
    return (201 if status == CONFIRMED else 202), registration


async def cancel(session: AsyncSession, registration_id: int) -> None:
    deleted = await session.execute(
        delete(Registration)
        .where(Registration.id == registration_id)
        .returning(Registration.section_id)
        .execution_options(synchronize_session=False)
    )
    row = deleted.first()
    if row is None:
        await session.rollback()
        raise HTTPException(404, "registration_not_found")
    await _release_seat(session, row.section_id)
    await session.commit()


async def _release_seat(session: AsyncSession, section_id: int) -> None:
    await session.execute(
        update(Section)
        .where(Section.id == section_id, Section.enrolled > 0)
        .values(enrolled=Section.enrolled - 1)
        .execution_options(synchronize_session=False)
    )


async def verify_pending(app: FastAPI) -> int:
    """Check tuition for registrations accepted while the payment service was unavailable.
    Updates are conditional on the status, so two replicas doing this at once are safe."""
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Registration.id, Registration.student_id, Section.term)
                .join(Section, Section.id == Registration.section_id)
                .where(Registration.status == PENDING)
                .order_by(Registration.id)
                .limit(50)
            )
        ).all()
    verified = 0
    for row in rows:
        try:
            paid = await _tuition_paid(app, row.student_id, row.term)
        except DependencyUnavailable:
            break
        async with sessionmaker() as session:
            if paid:
                done = await session.execute(
                    update(Registration)
                    .where(Registration.id == row.id, Registration.status == PENDING)
                    .values(status=CONFIRMED)
                    .returning(Registration.id)
                    .execution_options(synchronize_session=False)
                )
                event = "registration_verified"
            else:
                done = await session.execute(
                    delete(Registration)
                    .where(Registration.id == row.id, Registration.status == PENDING)
                    .returning(Registration.section_id)
                    .execution_options(synchronize_session=False)
                )
                event = "registration_rejected"
            changed = done.first()
            if changed is not None and not paid:
                await _release_seat(session, changed.section_id)
            await session.commit()
        if changed is not None:
            verified += 1
            logger.info(event, extra={"registration_id": row.id})
    return verified


async def refresh_timetable_cache(app: FastAPI) -> None:
    term = app.state.settings.term
    client = TimetableClient(app.state.http, app.state.settings.timetable_url)
    entries = await app.state.deps["timetable"].call(lambda: client.timetable(term))
    app.state.timetable_cache[term] = {e["section_id"]: e["timeslot_id"] for e in entries}


async def background_loop(app: FastAPI) -> None:
    """Keeps the timetable cache fresh and verifies pending registrations."""
    last_refresh = -CACHE_REFRESH_S
    loop = asyncio.get_running_loop()
    while True:
        try:
            if loop.time() - last_refresh >= CACHE_REFRESH_S:
                await refresh_timetable_cache(app)
                last_refresh = loop.time()
            await verify_pending(app)
        except Exception as exc:
            if _rate_log.should_log("background"):
                logger.warning("background_failed", extra={"error": type(exc).__name__})
        await asyncio.sleep(app.state.settings.worker_interval_s)
