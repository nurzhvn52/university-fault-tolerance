"""Request and response models of the records service and the transcript builder."""

import logging
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from common.validation import COURSE_CODE_PATTERN, TERM_PATTERN, StrictInput
from records.gpa import compute_gpa, voted_gpa
from records.models import Grade

logger = logging.getLogger("records.schemas")

Letter = Literal["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]


class GradeIn(StrictInput):
    student_id: int = Field(gt=0)
    course_code: str = Field(pattern=COURSE_CODE_PATTERN)
    term: str = Field(pattern=TERM_PATTERN)
    letter: Letter
    credits: int = Field(gt=0, le=30)


class GradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    course_code: str
    term: str
    letter: str
    points: Decimal
    credits: int


class GradeBatchIn(StrictInput):
    items: list[GradeIn] = Field(min_length=1, max_length=500)


class TranscriptOut(BaseModel):
    student_id: int
    gpa: Decimal
    credits: int
    grades: list[GradeOut]
    # FT: true when the database was unavailable and a cached copy was served.
    stale: bool = False


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    gpa: Decimal
    credits: int
    sha256: str
    created_at: datetime


def build_transcript(
    student_id: int, grades: Iterable[Grade], *, voting: bool = False
) -> TranscriptOut:
    grades = list(grades)
    pairs = [(grade.points, grade.credits) for grade in grades]
    if voting:
        (gpa, credits), outvoted = voted_gpa(pairs)
        if outvoted:
            logger.warning(
                "version_outvoted", extra={"student_id": student_id, "versions": outvoted}
            )
    else:
        gpa, credits = compute_gpa(pairs)
    return TranscriptOut(
        student_id=student_id,
        gpa=gpa,
        credits=credits,
        grades=[GradeOut.model_validate(grade) for grade in grades],
    )
