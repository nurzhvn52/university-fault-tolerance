"""Student service tables (schema "student")."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.db import schema_metadata


class Base(DeclarativeBase):
    metadata = schema_metadata("student")


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_no: Mapped[str] = mapped_column(String(16), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    program: Mapped[str] = mapped_column(String(80))
    year: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    credits: Mapped[int]


class Section(Base):
    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("student.courses.id"), index=True)
    term: Mapped[str] = mapped_column(String(16), index=True)
    capacity: Mapped[int]
    enrolled: Mapped[int] = mapped_column(default=0, server_default="0")


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("student_id", "section_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.students.id"), index=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("student.sections.id"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
