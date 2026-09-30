"""Timetable tables (schema "timetable")."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.db import schema_metadata


class Base(DeclarativeBase):
    metadata = schema_metadata("timetable")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    capacity: Mapped[int]


class Timeslot(Base):
    __tablename__ = "timeslots"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(3))
    starts_at: Mapped[str] = mapped_column(String(5))
    ends_at: Mapped[str] = mapped_column(String(5))


class TimetableEntry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        UniqueConstraint("term", "room_id", "timeslot_id"),
        UniqueConstraint("term", "section_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    term: Mapped[str] = mapped_column(String(16))
    # Section id from the student service; no foreign key across service schemas.
    section_id: Mapped[int]
    room_id: Mapped[int] = mapped_column(ForeignKey("timetable.rooms.id"))
    timeslot_id: Mapped[int] = mapped_column(ForeignKey("timetable.timeslots.id"))
    job_id: Mapped[int | None]


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    term: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16))
    sections_total: Mapped[int] = mapped_column(default=0, server_default="0")
    sections_done: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # FT: placements saved so far ({section_id: [room_id, timeslot_id]}), so a job can resume.
    checkpoint: Mapped[dict | None] = mapped_column(JSONB)
    # FT: the instance working on the job and until when; an expired lease can be taken over.
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
