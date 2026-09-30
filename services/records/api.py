"""Academic records API: grades, transcripts and stored transcript documents."""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from common.chaos import fault_point
from common.db import SessionDep, is_db_unavailable
from records import resilient
from records.gpa import GRADE_POINTS
from records.models import Grade, TranscriptDocument
from records.schemas import DocumentOut, GradeBatchIn, GradeOut, TranscriptOut, build_transcript

logger = logging.getLogger("records.api")
router = APIRouter(prefix="/api", tags=["records"])


async def _transcript(session, student_id: int, *, voting: bool = False) -> TranscriptOut:
    grades = await session.scalars(
        select(Grade).where(Grade.student_id == student_id).order_by(Grade.term, Grade.id)
    )
    return build_transcript(student_id, grades, voting=voting)


@router.get("/grades/{student_id}", response_model=list[GradeOut])
async def list_grades(student_id: int, session: SessionDep) -> list[GradeOut]:
    return (await _transcript(session, student_id)).grades


@router.post("/grades/batch", status_code=201)
async def import_grades(body: GradeBatchIn, request: Request, session: SessionDep) -> dict:
    if request.app.state.settings.ft:
        return {"imported": await resilient.import_grades(session, body.items)}
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
        await fault_point("records.grade_imported")
    return {"imported": imported}


@router.get("/transcripts/{student_id}", response_model=TranscriptOut)
async def get_transcript(student_id: int, request: Request, session: SessionDep):
    app = request.app
    if not app.state.settings.ft:
        return await _transcript(session, student_id)
    try:
        transcript = await _transcript(session, student_id, voting=True)
    except Exception as exc:
        stale = resilient.cached(app, student_id) if is_db_unavailable(exc) else None
        if stale is None:
            raise
        return JSONResponse(stale.model_dump(mode="json"), headers={"X-Data-Stale": "true"})
    if transcript.grades:
        resilient.store(app, transcript)
    return transcript


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
