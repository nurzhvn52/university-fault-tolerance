import asyncio
import time

import jwt
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from common.app import create_service_app
from common.audit import AuditLog
from common.config import Settings
from common.ratelimit import RateLimiter
from common.security import (
    Authenticated,
    ConfigError,
    Staff,
    StudentAccess,
    TokenError,
    authorize_student,
    decode_token,
    issue_token,
    validate_config,
)
from common.validation import StrictInput

KEY = "k" * 40
OTHER_KEY = "o" * 40


def token(role="student", sub="s1", student_id=1, key=KEY, **kwargs):
    return issue_token([key], sub=sub, role=role, student_id=student_id, **kwargs)


def reason(tok: str, keys=(KEY,)) -> str:
    with pytest.raises(TokenError) as exc:
        decode_token(list(keys), tok)
    return exc.value.reason


# ---------------------------------------------------------------- tokens


def test_valid_token_roundtrip():
    principal = decode_token([KEY], token(role="student", sub="250001", student_id=7))
    assert (principal.sub, principal.role, principal.student_id) == ("250001", "student", 7)


def test_token_attacks_are_rejected():
    now = time.time()
    good = token()
    assert reason(token(key=OTHER_KEY)) == "invalid_token"
    assert reason(good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")) == "invalid_token"
    assert reason(token(now=now - 7200, ttl_s=900)) == "expired_token"
    claims = {
        "iss": "uft-auth",
        "aud": "uft",
        "sub": "x",
        "role": "admin",
        "iat": 1,
        "exp": now + 99,
    }
    unsigned = jwt.encode(claims | {"jti": "j"}, key=None, algorithm="none")
    assert reason(unsigned) == "invalid_token"
    wrong_audience = jwt.encode(
        claims | {"jti": "j", "aud": "other", "iat": int(now)}, KEY, algorithm="HS256"
    )
    assert reason(wrong_audience) == "invalid_token"
    no_exp = jwt.encode({k: v for k, v in claims.items() if k != "exp"} | {"jti": "j"}, KEY)
    assert reason(no_exp) == "invalid_token"
    student_without_id = issue_token([KEY], sub="x", role="student")
    assert reason(student_without_id) == "invalid_token"


def test_key_rotation_old_key_verifies_but_does_not_sign():
    old_token = token(key="a" * 40)
    rotated = ["n" * 40, "a" * 40]
    assert decode_token(rotated, old_token).sub == "s1"
    assert jwt.get_unverified_header(issue_token(rotated, sub="x", role="admin"))["alg"] == "HS256"
    assert reason(issue_token(rotated, sub="x", role="admin"), keys=["a" * 40]) == "invalid_token"


@pytest.mark.parametrize("secret", ["", "short", "changeme", "uft_local_only"])
def test_weak_jwt_secret_stops_the_service(secret):
    settings = Settings(service_name="student", security=True, jwt_secret=secret)
    with pytest.raises(ConfigError):
        validate_config(settings)


def test_config_check_accepts_strong_secret_and_ignores_midterm_mode():
    assert validate_config(Settings(service_name="student", security=True, jwt_secret=KEY)) == [KEY]
    assert validate_config(Settings(service_name="student", security=False)) == []
    with pytest.raises(ConfigError):
        validate_config(Settings(service_name="payment", security=True, jwt_secret=KEY))


# ---------------------------------------------------------------- authorization over HTTP

router = APIRouter(prefix="/api")


@router.get("/students/{student_id}")
async def get_student(student_id: int, principal: StudentAccess) -> dict:
    return {"id": student_id, "by": principal.sub}


@router.get("/staff")
async def staff(principal: Staff) -> dict:
    return {"by": principal.sub}


@router.get("/any")
async def any_user(principal: Authenticated) -> dict:
    return {"by": principal.sub}


class Pay(StrictInput):
    student_id: int


@router.post("/pay")
async def pay(body: Pay, request: Request, principal: Authenticated) -> dict:
    authorize_student(request, principal, body.student_id)
    return {"ok": True}


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr("common.validation.get_settings", lambda: Settings(security=True))
    settings = Settings(
        service_name="sectest",
        security=True,
        jwt_secret=KEY,
        rate_limit_rps=0.001,
        rate_limit_burst=5,
        max_body_bytes=200,
    )
    app = create_service_app(settings, title="t", routers=[router], use_db=False)
    with TestClient(app) as client:
        events = []
        real_record = app.state.audit.record

        def record(action, outcome, **kwargs):
            events.append((action, outcome, kwargs.get("actor"), kwargs.get("detail")))
            return real_record(action, outcome, **kwargs)

        app.state.audit.record = record
        client.events = events
        yield client


def bearer(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def test_missing_and_malformed_credentials_get_401_and_an_audit_event(api, caplog):
    caplog.set_level("INFO", logger="audit")
    assert api.get("/api/any").status_code == 401
    assert api.get("/api/any", headers={"Authorization": "Basic abc"}).status_code == 401
    assert api.get("/api/any", headers=bearer("not.a.jwt")).status_code == 401
    logged = [r.detail["reason"] for r in caplog.records if r.name == "audit"]
    assert logged == ["missing_token", "invalid_token"]  # the repeat is collapsed


def test_student_sees_only_own_data_staff_and_services_see_all(api):
    own = bearer(token(sub="s1", student_id=1))
    assert api.get("/api/students/1", headers=own).status_code == 200
    other = api.get("/api/students/2", headers=own)
    assert other.status_code == 403 and other.json()["detail"] == "forbidden_owner"
    assert (
        api.get("/api/students/2", headers=bearer(token("registrar", student_id=None))).status_code
        == 200
    )
    assert (
        api.get("/api/students/2", headers=bearer(token("service", student_id=None))).status_code
        == 200
    )
    denied = [e for e in api.events if e[1] == "denied"]
    assert denied and denied[0][2] == "s1"


def test_staff_routes_refuse_students_and_services(api):
    assert api.get("/api/staff", headers=bearer(token("student"))).status_code == 403
    assert (
        api.get("/api/staff", headers=bearer(token("service", student_id=None))).status_code == 403
    )
    assert api.get("/api/staff", headers=bearer(token("admin", student_id=None))).status_code == 200


def test_body_identity_is_checked_and_unknown_fields_rejected(api):
    own = bearer(token(sub="s1", student_id=1))
    assert api.post("/api/pay", json={"student_id": 1}, headers=own).status_code == 200
    assert api.post("/api/pay", json={"student_id": 2}, headers=own).status_code == 403
    extra = api.post("/api/pay", json={"student_id": 1, "role": "admin"}, headers=own)
    assert extra.status_code == 422


def test_unauthenticated_request_never_reaches_body_validation(api):
    response = api.post("/api/pay", json={"student_id": "not a number"})
    assert response.status_code == 401


def test_rate_limit_is_per_principal_and_exempts_services(api):
    first = bearer(token(sub="busy", student_id=1))
    codes = [api.get("/api/students/1", headers=first).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}
    assert (
        api.get("/api/students/2", headers=bearer(token(sub="calm", student_id=2))).status_code
        == 200
    )
    service = bearer(token("service", sub="svc:x", student_id=None))
    assert {api.get("/api/any", headers=service).status_code for _ in range(20)} == {200}
    limited = api.get("/api/students/1", headers=first)
    assert limited.headers["retry-after"] == "1"


def test_oversized_body_is_refused_before_it_is_read(api):
    headers = bearer(token(sub="big", student_id=1))
    response = api.post(
        "/api/pay", content=b"x" * 500, headers=headers | {"Content-Type": "application/json"}
    )
    assert response.status_code == 413


# ---------------------------------------------------------------- units


def test_token_bucket_refills_and_prunes_idle_keys():
    now = [0.0]
    limiter = RateLimiter(rate=2.0, burst=3, clock=lambda: now[0], max_keys=2)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    now[0] = 1.0  # two tokens came back
    assert [limiter.allow("a") for _ in range(3)] == [True, True, False]
    limiter.allow("b")
    now[0] = 100.0
    limiter.allow("c")  # table is full: idle buckets are dropped
    assert set(limiter._buckets) == {"c"}


def test_strict_input_is_only_strict_when_security_is_on(monkeypatch):
    monkeypatch.setattr("common.validation.get_settings", lambda: Settings(security=False))
    assert Pay.model_validate({"student_id": 1, "extra": 1}).student_id == 1
    monkeypatch.setattr("common.validation.get_settings", lambda: Settings(security=True))
    with pytest.raises(ValidationError):
        Pay.model_validate({"student_id": 1, "extra": 1})


class FakeEngine:
    """Database that fails until ``up`` is set."""

    def __init__(self):
        self.up = False
        self.rows = []

    def begin(self):
        engine = self

        class Ctx:
            async def __aenter__(self):
                if not engine.up:
                    raise ConnectionError("db down")
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, statement, batch):
                engine.rows.extend(batch)

        return Ctx()


def test_audit_events_survive_a_database_outage_and_repeats_are_collapsed():
    async def scenario():
        engine = FakeEngine()
        audit = AuditLog("t", engine, queue_size=50)
        audit.start()
        for i in range(5):
            audit.record("payment.create", "ok", actor="s1", resource=f"p{i}")
        await asyncio.sleep(0.2)
        assert engine.rows == []  # database is down, events are kept
        engine.up = True
        await asyncio.sleep(1.5)
        stored = [r["resource"] for r in engine.rows]
        for _ in range(10):
            audit.record("auth.denied", "denied", actor="x", dedup=("r", "x"))
        await audit.stop()
        return stored, [r for r in engine.rows if r["action"] == "auth.denied"]

    stored, denied = asyncio.run(scenario())
    assert stored == [f"p{i}" for i in range(5)]
    assert len(denied) == 1  # nine repeats within the window were suppressed


def test_audit_buffer_is_bounded_and_counts_dropped_events():
    from common.telemetry import AUDIT_DROPPED

    audit = AuditLog("dropper", FakeEngine(), queue_size=3)
    for i in range(7):
        audit.record("x", "ok", resource=str(i))
    assert [e["resource"] for e in audit._buffer] == ["4", "5", "6"]
    assert AUDIT_DROPPED.labels("dropper")._value.get() == 4
