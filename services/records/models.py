"""Academic records tables (schema "records")."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.db import schema_metadata


class Base(DeclarativeBase):
    metadata = schema_metadata("records")


class Grade(Base):
    __tablename__ = "grades"
    __table_args__ = (UniqueConstraint("student_id", "course_code", "term"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int]
    course_code: Mapped[str] = mapped_column(String(16))
    term: Mapped[str] = mapped_column(String(16))
    letter: Mapped[str] = mapped_column(String(2))
    points: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    credits: Mapped[int]


class TranscriptDocument(Base):
    """A generated transcript file; the file itself lives in transcript storage."""

    __tablename__ = "transcript_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(index=True)
    gpa: Mapped[Decimal] = mapped_column(Numeric(4, 2))
    credits: Mapped[int]
    storage_key: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
