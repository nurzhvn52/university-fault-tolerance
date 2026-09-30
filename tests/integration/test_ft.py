"""End-to-end checks of the software fault-tolerance mechanisms (FT mode).

UFT_FT_MODE=ft docker compose -f docker-compose.baseline.yml up -d --build
UFT_FT_MODE=ft UFT_BASE_URL=http://localhost:8080 pytest -m integration
"""

import asyncio
import os
import time
import uuid
from decimal import Decimal

import httpx
import pytest

BASE_URL = os.environ.get("UFT_BASE_URL")
TERM = "2026-FALL"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not BASE_URL or os.environ.get("UFT_FT_MODE") != "ft",
        reason="needs a running stack in FT mode (UFT_BASE_URL, UFT_FT_MODE=ft)",
    ),
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


def unpaid_students(api, count):
    invoices = api.get("/api/tuition", params={"term": TERM, "paid": False, "limit": 50}).json()
    return [invoice["student_id"] for invoice in invoices[-count:]]


def test_parallel_duplicates_of_one_payment_charge_once(api):
    (student_id,) = unpaid_students(api, 1)
    before = api.get(f"/api/tuition/{student_id}", params={"term": TERM}).json()
    body = {"student_id": student_id, "term": TERM, "amount": "1000.00"}
    key = uuid.uuid4().hex

    async def pay(client):
        return await client.post("/api/payments", json=body, headers={"Idempotency-Key": key})

    async def five_at_once():
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as client:
            return await asyncio.gather(*(pay(client) for _ in range(5)))

    responses = asyncio.run(five_at_once())

    assert all(r.status_code in (200, 201, 202) for r in responses), [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    time.sleep(1)
    after = api.get(f"/api/tuition/{student_id}", params={"term": TERM}).json()
    assert Decimal(after["amount_paid"]) - Decimal(before["amount_paid"]) == Decimal("1000.00")


def test_repeated_payment_returns_the_original(api):
    (student_id,) = unpaid_students(api, 1)
    body = {"student_id": student_id, "term": TERM, "amount": "500.00"}
    headers = {"Idempotency-Key": uuid.uuid4().hex}

    first = api.post("/api/payments", json=body, headers=headers)
    again = api.post("/api/payments", json=body, headers=headers)

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == first.json()


def test_grade_import_can_be_repeated_safely(api):
    term = f"T{uuid.uuid4().hex[:8]}"
    items = [
        {"student_id": i, "course_code": "CSE316", "term": term, "letter": "A", "credits": 5}
        for i in range(1, 21)
    ]

    for _ in range(2):
        response = api.post("/api/grades/batch", json={"items": items})
        assert response.status_code == 201
        assert response.json() == {"imported": 20}

    grades = api.get("/api/grades/1").json()
    assert sum(g["term"] == term for g in grades) == 1


def test_second_generation_request_returns_the_running_job(api):
    first = api.post("/api/timetable/generate", params={"term": TERM})
    second = api.post("/api/timetable/generate", params={"term": TERM})

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    deadline = time.monotonic() + 60
    job = first.json()
    while job["status"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(1)
        job = api.get(f"/api/timetable/jobs/{job['id']}").json()
    assert job["status"] == "COMPLETED"


def test_concurrent_registrations_never_overbook(api):
    sections = api.get("/api/sections", params={"term": TERM}).json()
    section = next(s for s in sections if s["enrolled"] == 0 and s["capacity"] <= 40)
    paid = api.get("/api/tuition", params={"term": TERM, "paid": True, "limit": 300}).json()
    students = [p["student_id"] for p in paid][-2 * section["capacity"] :]

    async def register_all():
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
            return await asyncio.gather(
                *(
                    client.post(
                        "/api/registrations",
                        json={"student_id": sid, "section_id": section["id"]},
                    )
                    for sid in students
                )
            )

    responses = asyncio.run(register_all())
    accepted = [r for r in responses if r.status_code in (201, 202)]
    after = api.get(f"/api/sections/{section['id']}").json()

    assert len(accepted) <= section["capacity"]
    assert after["enrolled"] == len(accepted)
    for response in accepted:
        api.delete(f"/api/registrations/{response.json()['id']}")
    assert api.get(f"/api/sections/{section['id']}").json()["enrolled"] == 0
