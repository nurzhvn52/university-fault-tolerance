"""Academic records API: grades, transcripts and stored transcript documents."""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from common.db import SessionDep
from records.gpa import GRADE_POINTS, compute_gpa
from records.models import Grade, TranscriptDocument

logger = logging.getLogger("records.api")
router = APIRouter(prefix="/api", tags=["records"])

Letter = Literal["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]


class GradeIn(BaseModel):
    student_id: int = Field(gt=0)
    course_code: str = Field(max_length=16)
    term: str = Field(max_length=16)
    letter: Letter
    credits: int = Field(gt=0, le=30)


class GradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    course_code: str
    term: str
    letter: str
    points: Decimal
    credits: int


class GradeBatchIn(BaseModel):
    items: list[GradeIn] = Field(min_length=1, max_length=500)


class TranscriptOut(BaseModel):
    student_id: int
    gpa: Decimal
    credits: int
    grades: list[GradeOut]


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    gpa: Decimal
    credits: int
    sha256: str
    created_at: datetime


async def _transcript(session, student_id: int) -> TranscriptOut:
    grades = list(
        await session.scalars(
            select(Grade).where(Grade.student_id == student_id).order_by(Grade.term, Grade.id)
        )
    )
    gpa, credits = compute_gpa((grade.points, grade.credits) for grade in grades)
    return TranscriptOut(
        student_id=student_id,
        gpa=gpa,
        credits=credits,
        grades=[GradeOut.model_validate(grade) for grade in grades],
    )


@router.get("/grades/{student_id}", response_model=list[GradeOut])
async def list_grades(student_id: int, session: SessionDep) -> list[GradeOut]:
    return (await _transcript(session, student_id)).grades


@router.post("/grades/batch", status_code=201)
async def import_grades(body: GradeBatchIn, session: SessionDep) -> dict:
    # Baseline: every grade is committed on its own. If the import stops half way, the
    # rows written so far stay, and re-sending the batch fails on the first duplicate.
    imported = 0
    for item in body.items:
        session.add(Grade(**item.model_dump(), points=GRADE_POINTS[item.letter]))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return JSONResponse(
                status_code=409,
                content={"error": "duplicate_grade", "imported": imported, "at": item.model_dump()},
            )
        imported += 1
    return {"imported": imported}


@router.get("/transcripts/{student_id}", response_model=TranscriptOut)
async def get_transcript(student_id: int, session: SessionDep) -> TranscriptOut:
    return await _transcript(session, student_id)


@router.post("/transcripts/{student_id}/documents", response_model=DocumentOut, status_code=201)
async def create_document(
    student_id: int, request: Request, session: SessionDep
) -> TranscriptDocument:
    transcript = await _transcript(session, student_id)
    if not transcript.grades:
        raise HTTPException(404, "no_grades")
    content = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **transcript.model_dump(mode="json"),
    }
    data = json.dumps(content, sort_keys=True).encode()
    key = f"{student_id}/{uuid.uuid4().hex}.json"
    request.app.state.storage.write(key, data)
    document = TranscriptDocument(
        student_id=student_id,
        gpa=transcript.gpa,
        credits=transcript.credits,
        storage_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
    )
    session.add(document)
    await session.commit()
    logger.info("transcript_document_created", extra={"document_id": document.id, "key": key})
    return document


@router.get("/transcripts/{student_id}/documents/latest")
async def latest_document(student_id: int, request: Request, session: SessionDep) -> dict:
    document = await session.scalar(
        select(TranscriptDocument)
        .where(TranscriptDocument.student_id == student_id)
        .order_by(TranscriptDocument.id.desc())
        .limit(1)
    )
    if document is None:
        raise HTTPException(404, "document_not_found")
    # Baseline: the stored bytes are returned as they are, without checking the hash.
    data = request.app.state.storage.read(document.storage_key)
    return {"document_id": document.id, "sha256": document.sha256, "content": json.loads(data)}
