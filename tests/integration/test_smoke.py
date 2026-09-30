"""End-to-end checks through the gateway of a running stack.

docker compose -f docker-compose.baseline.yml up -d --build
UFT_BASE_URL=http://localhost:8080 pytest -m integration
"""

import os
import time
from decimal import Decimal

import httpx
import pytest

BASE_URL = os.environ.get("UFT_BASE_URL")
TERM = "2026-FALL"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not BASE_URL, reason="UFT_BASE_URL is not set"),
]


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        deadline = time.monotonic() + 300
        while True:
            try:
                if client.get("/api/courses").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("the stack did not become ready")
            time.sleep(1)
        yield client


def first_student(api, *, paid: bool) -> int:
    invoices = api.get("/api/tuition", params={"term": TERM, "paid": paid, "limit": 1}).json()
    assert invoices, f"seed data has no {'paid' if paid else 'unpaid'} invoices"
    return invoices[0]["student_id"]


def test_catalogue_and_student(api):
    assert len(api.get("/api/courses").json()) == 40
    assert len(api.get("/api/sections", params={"term": TERM}).json()) == 120
    student = api.get("/api/students/1")
    assert student.status_code == 200
    assert student.headers["X-Instance"].startswith("student-")
    assert api.get("/api/students/999999").status_code == 404


def test_tuition_payment_in_two_installments(api):
    student_id = first_student(api, paid=False)
    before = api.get(f"/api/tuition/{student_id}", params={"term": TERM}).json()
    half = Decimal(before["balance"]) / 2

    for _ in range(2):
        payment = api.post(
            "/api/payments", json={"student_id": student_id, "term": TERM, "amount": str(half)}
        )
        assert payment.status_code == 201, payment.text
        assert payment.json()["status"] == "CAPTURED"

    after = api.get(f"/api/tuition/{student_id}", params={"term": TERM}).json()
    assert after["paid"] is True
    assert Decimal(after["balance"]) == 0
    assert len(api.get("/api/payments", params={"student_id": student_id}).json()) == 2
    overpay = api.post(
        "/api/payments", json={"student_id": student_id, "term": TERM, "amount": "1"}
    )
    assert overpay.status_code == 409


def test_registration_requires_paid_tuition(api):
    student_id = first_student(api, paid=False)
    response = api.post("/api/registrations", json={"student_id": student_id, "section_id": 1})
    assert response.status_code == 402


def test_register_and_cancel(api):
    student_id = first_student(api, paid=True)
    sections = api.get("/api/sections", params={"term": TERM}).json()
    section = next(s for s in sections if s["enrolled"] < s["capacity"])

    registration = api.post(
        "/api/registrations", json={"student_id": student_id, "section_id": section["id"]}
    )
    assert registration.status_code == 201, registration.text
    duplicate = api.post(
        "/api/registrations", json={"student_id": student_id, "section_id": section["id"]}
    )
    assert duplicate.status_code == 409
    assert api.get(f"/api/sections/{section['id']}").json()["enrolled"] == section["enrolled"] + 1

    registration_id = registration.json()["id"]
    assert api.delete(f"/api/registrations/{registration_id}").status_code == 204
    assert api.get(f"/api/sections/{section['id']}").json()["enrolled"] == section["enrolled"]


def test_transcript_document_is_stored_and_read_back(api):
    student_id = next(i for i in range(1, 50) if api.get(f"/api/students/{i}").json()["year"] > 1)
    transcript = api.get(f"/api/transcripts/{student_id}").json()
    assert transcript["grades"]

    document = api.post(f"/api/transcripts/{student_id}/documents")
    assert document.status_code == 201
    latest = api.get(f"/api/transcripts/{student_id}/documents/latest").json()
    assert latest["document_id"] == document.json()["id"]
    assert latest["content"]["gpa"] == transcript["gpa"]


def test_timetable_generation_job_completes(api):
    job = api.post("/api/timetable/generate", params={"term": TERM})
    assert job.status_code == 202
    job_id = job.json()["id"]

    deadline = time.monotonic() + 60
    status = job.json()
    while status["status"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(1)
        status = api.get(f"/api/timetable/jobs/{job_id}").json()

    assert status["status"] == "COMPLETED", status
    assert status["sections_done"] == status["sections_total"] == 120
    assert len(api.get("/api/timetable", params={"term": TERM}).json()) == 120
