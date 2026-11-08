"""Security checks through the gateway of the endterm stack (UFT_SECURITY=true).

    python -m lab.init_secrets
    UFT_TRACING=true UFT_SECURITY=true docker compose -f docker-compose.ft.yml \
        -f docker-compose.endterm.yml --profile monitoring up -d --build
    UFT_BASE_URL=http://localhost:8080 pytest -m integration tests/integration/test_endterm_security.py
"""

import concurrent.futures
import os
import threading
import time

import httpx
import jwt
import pytest

BASE_URL = os.environ.get("UFT_BASE_URL")
PASSWORD = os.environ.get("UFT_SEED_PASSWORD", "synthetic-lab-password")
TERM = "2026-FALL"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not BASE_URL, reason="UFT_BASE_URL is not set"),
]


def login(client: httpx.Client, username: str, password: str = PASSWORD) -> httpx.Response:
    return client.post("/api/auth/login", json={"username": username, "password": password})


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        deadline = time.monotonic() + 300
        while True:
            try:
                if login(client, "admin1").status_code in (200, 401, 404):
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("the stack did not become ready")
            time.sleep(1)
        if login(client, "admin1").status_code != 200:
            pytest.skip("the stack runs without the security layer")
        yield client


@pytest.fixture(scope="module")
def admin(api):
    return bearer(login(api, "admin1").json()["access_token"])


def student_session(api, admin, student_id: int) -> dict:
    number = api.get(f"/api/students/{student_id}", headers=admin).json()["student_no"]
    response = login(api, number)
    assert response.status_code == 200, response.text
    return bearer(response.json()["access_token"])


def test_requests_without_valid_credentials_are_refused(api, admin):
    paths = [
        ("GET", "/api/students/1"),
        ("GET", f"/api/tuition/1?term={TERM}"),
        ("GET", "/api/transcripts/2"),
        ("POST", "/api/timetable/generate?term=" + TERM),
    ]
    forged = jwt.encode({"sub": "x", "role": "admin"}, "w" * 40, algorithm="HS256")
    unsigned = jwt.encode({"sub": "x", "role": "admin"}, key=None, algorithm="none")
    for method, path in paths:
        for headers in ({}, bearer("garbage"), bearer(forged), bearer(unsigned)):
            assert api.request(method, path, headers=headers).status_code == 401, (method, path)


def test_student_reaches_only_own_data(api, admin):
    mine = student_session(api, admin, 3)
    assert api.get("/api/students/3", headers=mine).status_code == 200
    assert api.get(f"/api/tuition/3?term={TERM}", headers=mine).status_code == 200
    for path in (
        "/api/students/4",
        "/api/students/4/registrations",
        f"/api/tuition/4?term={TERM}",
        "/api/payments?student_id=4",
        "/api/grades/4",
        "/api/transcripts/4",
        "/api/transcripts/4/documents/latest",
    ):
        assert api.get(path, headers=mine).status_code == 403, path
    other = api.post("/api/registrations", json={"student_id": 4, "section_id": 1}, headers=mine)
    assert other.status_code == 403
    assert api.post("/api/transcripts/4/documents", headers=mine).status_code == 403


def test_student_cannot_use_staff_operations(api, admin):
    mine = student_session(api, admin, 5)
    batch = {
        "items": [
            {
                "student_id": 5,
                "course_code": "CSE316",
                "term": "2026-HACK",
                "letter": "A",
                "credits": 5,
            }
        ]
    }
    assert api.post("/api/grades/batch", json=batch, headers=mine).status_code == 403
    assert api.post(f"/api/timetable/generate?term={TERM}", headers=mine).status_code == 403
    assert api.get(f"/api/tuition?term={TERM}", headers=mine).status_code == 403
    assert api.get(f"/api/tuition?term={TERM}", headers=admin).status_code == 200


def test_service_endpoints_and_chaos_are_not_reachable_from_outside(api, admin):
    assert api.post("/_chaos/crash").status_code == 404
    assert api.get("/metrics").status_code == 404
    assert api.get("/health/ready").status_code == 404


def test_malformed_and_oversized_input_is_rejected(api, admin):
    mine = student_session(api, admin, 6)
    assert api.post("/api/registrations", content=b"{not json", headers=mine).status_code == 422
    unknown = {"student_id": 6, "section_id": 1, "role": "admin"}
    assert api.post("/api/registrations", json=unknown, headers=mine).status_code == 422
    bad_amount = {"student_id": 6, "term": TERM, "amount": "-5"}
    assert api.post("/api/payments", json=bad_amount, headers=mine).status_code == 422
    sqli = {"student_id": 6, "term": "2026-FALL'; DROP TABLE x;--", "amount": "10"}
    assert api.post("/api/payments", json=sqli, headers=mine).status_code == 422
    short_key = {**{"student_id": 6, "term": TERM, "amount": "10"}}
    assert (
        api.post("/api/payments", json=short_key, headers=mine | {"Idempotency-Key": "x"})
    ).status_code == 422
    huge = b'{"student_id": 6, "pad": "' + b"a" * 300_000 + b'"}'
    assert api.post("/api/registrations", content=huge, headers=mine).status_code == 413


def test_rate_limit_throttles_one_client_not_the_others(api, admin):
    busy = student_session(api, admin, 7)
    calm = student_session(api, admin, 8)

    local = threading.local()

    def hit(_):
        if not hasattr(local, "client"):
            local.client = httpx.Client(base_url=BASE_URL, timeout=15)
        return local.client.get("/api/students/7", headers=busy).status_code

    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(hit, range(400)))
    assert 429 in codes and 200 in codes
    assert api.get("/api/students/8", headers=calm).status_code == 200


def test_repeated_wrong_passwords_lock_the_account(api, admin):
    number = api.get("/api/students/50", headers=admin).json()["student_no"]
    codes = [login(api, number, "wrong-password").status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and set(codes[5:]) == {429}
    # The right password does not unlock it early, and an unknown user looks like a bad password.
    assert login(api, number).status_code == 429
    assert login(api, "nobody-here", "x").status_code == 401


def test_request_id_is_returned_and_kept(api, admin):
    response = api.get("/api/students/1", headers=admin | {"X-Request-ID": "trace-test-000001"})
    assert response.headers["x-request-id"] == "trace-test-000001"
    assert api.get("/api/students/1", headers=admin).headers["x-request-id"]


def test_legitimate_workflow_works_with_service_tokens_and_bank_key(api, admin):
    invoices = api.get("/api/tuition", params={"term": TERM, "limit": 1000}, headers=admin).json()
    paid = next(i for i in invoices if i["paid"] and i["student_id"] <= 40)["student_id"]
    unpaid = next(i for i in invoices if not i["paid"] and i["student_id"] <= 40)
    student = student_session(api, admin, paid)
    registration = api.post(
        "/api/registrations", json={"student_id": paid, "section_id": 110}, headers=student
    )
    assert registration.status_code in (201, 202), registration.text

    payer = student_session(api, admin, unpaid["student_id"])
    body = {"student_id": unpaid["student_id"], "term": TERM, "amount": unpaid["balance"]}
    headers = payer | {"Idempotency-Key": f"endterm-test-{unpaid['student_id']}"}
    first = api.post("/api/payments", json=body, headers=headers)
    assert first.status_code in (201, 202), first.text
    again = api.post("/api/payments", json=body, headers=headers)
    assert again.status_code in (200, 202) and again.json()["id"] == first.json()["id"]
