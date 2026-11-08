"""Controlled misuse of the lab system: unauthorized access, malformed input, floods and
password guessing. Only synthetic data of the lab stack is targeted.

Every probe records what it sent, the status it got and whether the answer leaked data of a
*victim* (a student the legitimate load never touches). The run compares the protected state
of the victims before and after (``lab.security_check``), so "no data changed" is measured,
not assumed.

    python -m lab.attacker login ...     # runs in its own container (own source address)
"""

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

from lab.metrics import percentile
from lab.tokens import Tokens

TERM = "2026-FALL"
BLOCKED = {400, 401, 403, 404, 405, 411, 413, 422, 429}


@dataclass
class Probe:
    name: str
    method: str
    path: str
    # Statuses meaning "the system refused": anything 2xx means the attack worked.
    expect: set[int]
    headers: dict = field(default_factory=dict)
    json: dict | None = None
    content: bytes | None = None
    # A 200 with an empty list is a safe answer too (a query that matched nothing).
    empty_ok: bool = False


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") or body.get("error") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else "validation_error" if detail else ""


async def run_probes(
    base_url: str, probes: list[Probe], markers: list[str], pace_s: float = 0.1
) -> dict:
    """Sends the probes one after another; ``markers`` are strings that must never appear in
    an answer (names and numbers of victims)."""
    results = []
    async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
        for probe in probes:
            started = time.time()
            try:
                response = await client.request(
                    probe.method,
                    probe.path,
                    headers=probe.headers,
                    json=probe.json,
                    content=probe.content,
                )
                status, body = response.status_code, response.text
                reason = _reason(response)
            except httpx.HTTPError as exc:
                status, body, reason = 0, "", type(exc).__name__
            results.append(
                {
                    "name": probe.name,
                    "method": probe.method,
                    "path": probe.path[:80],
                    "status": status,
                    "reason": reason,
                    "expected": sorted(probe.expect),
                    "refused": status in probe.expect
                    or (probe.empty_ok and status == 200 and body.strip() == "[]"),
                    "succeeded": 200 <= status < 300
                    and not (probe.empty_ok and body.strip() == "[]"),
                    "leaked": 200 <= status < 300 and any(m in body for m in markers),
                    "server_error": status >= 500 or status == 0,
                    "t": started,
                }
            )
            await asyncio.sleep(pace_s)
    return summarize(results)


def summarize(results: list[dict]) -> dict:
    return {
        "probes": results,
        "total": len(results),
        "refused": sum(r["refused"] for r in results),
        "succeeded": sum(r["succeeded"] for r in results),
        "leaked": sum(r["leaked"] for r in results),
        "server_errors": sum(r["server_error"] for r in results),
        "statuses": dict(Counter(str(r["status"]) for r in results)),
        "reasons": sorted({r["reason"] for r in results if r["reason"]}),
    }


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def unauthorized_probes(tokens: Tokens, attacker: int, victim: int) -> list[Probe]:
    me = bearer(tokens.attacker(attacker))
    v = victim
    idem = {"Idempotency-Key": f"attack-{attacker}-{v}-0001"}
    poisoned = {
        "items": [
            {
                "student_id": v,
                "course_code": "CSE316",
                "term": "2026-HACK",
                "letter": "F",
                "credits": 5,
            }
        ]
    }
    return [
        Probe("no_token", "GET", f"/api/students/{v}", {401}),
        Probe("garbage_token", "GET", f"/api/students/{v}", {401}, bearer("garbage")),
        Probe("forged_signature", "GET", f"/api/students/{v}", {401}, bearer(tokens.wrong_key())),
        Probe("alg_none", "GET", f"/api/students/{v}", {401}, bearer(_alg_none())),
        Probe(
            "expired_token",
            "GET",
            f"/api/students/{attacker}",
            {401},
            bearer(tokens.expired_student(attacker)),
        ),
        Probe("idor_student", "GET", f"/api/students/{v}", {403}, me),
        Probe("idor_registrations", "GET", f"/api/students/{v}/registrations", {403}, me),
        Probe("idor_tuition", "GET", f"/api/tuition/{v}?term={TERM}", {403}, me),
        Probe("idor_payments", "GET", f"/api/payments?student_id={v}", {403}, me),
        Probe("idor_grades", "GET", f"/api/grades/{v}", {403}, me),
        Probe("idor_transcript", "GET", f"/api/transcripts/{v}", {403}, me),
        Probe("idor_document", "GET", f"/api/transcripts/{v}/documents/latest", {403}, me),
        Probe(
            "idor_register",
            "POST",
            "/api/registrations",
            {403},
            me,
            json={"student_id": v, "section_id": 1},
        ),
        Probe(
            "idor_pay",
            "POST",
            "/api/payments",
            {403},
            me | idem,
            json={"student_id": v, "term": TERM, "amount": "1.00"},
        ),
        Probe("idor_document_create", "POST", f"/api/transcripts/{v}/documents", {403}, me),
        Probe("staff_grade_import", "POST", "/api/grades/batch", {403}, me, json=poisoned),
        Probe(
            "staff_timetable_generate", "POST", f"/api/timetable/generate?term={TERM}", {403}, me
        ),
        Probe("staff_invoice_list", "GET", f"/api/tuition?term={TERM}&limit=5", {403}, me),
        Probe("internal_chaos", "POST", "/_chaos/crash", {404}, me),
        Probe("internal_metrics", "GET", "/metrics", {404}, me),
        Probe("internal_health", "GET", "/health/ready", {404}, me),
    ]


def malformed_probes(tokens: Tokens, attacker: int) -> list[Probe]:
    me = bearer(tokens.attacker(attacker)) | {"Content-Type": "application/json"}
    pay = {"student_id": attacker, "term": TERM, "amount": "10.00"}
    idem = {"Idempotency-Key": f"malformed-{attacker}-0001"}
    return [
        Probe("invalid_json", "POST", "/api/registrations", {422}, me, content=b"{not json"),
        Probe(
            "wrong_type",
            "POST",
            "/api/registrations",
            {422},
            me,
            json={"student_id": "abc", "section_id": 1},
        ),
        Probe(
            "unknown_field_role",
            "POST",
            "/api/registrations",
            {422},
            me,
            json={"student_id": attacker, "section_id": 1, "role": "admin"},
        ),
        Probe(
            "negative_amount",
            "POST",
            "/api/payments",
            {422},
            me | idem,
            json=pay | {"amount": "-5"},
        ),
        Probe("zero_amount", "POST", "/api/payments", {422}, me | idem, json=pay | {"amount": "0"}),
        Probe(
            "amount_above_limit",
            "POST",
            "/api/payments",
            {422},
            me | idem,
            json=pay | {"amount": "99999999.00"},
        ),
        Probe(
            "sql_injection_term",
            "POST",
            "/api/payments",
            {422},
            me | idem,
            json=pay | {"term": "x' OR '1'='1"},
        ),
        Probe(
            "long_idempotency_key",
            "POST",
            "/api/payments",
            {422},
            me | {"Idempotency-Key": "k" * 300},
            json=pay,
        ),
        Probe(
            "oversized_body",
            "POST",
            "/api/registrations",
            {413},
            me,
            content=b'{"student_id": 1, "pad": "' + b"a" * 300_000 + b'"}',
        ),
        Probe(
            "sql_injection_ids",
            "GET",
            f"/api/timetable/sections?term={TERM}&ids=1;DROP TABLE x",
            {422},
            me,
        ),
        Probe("method_misuse", "DELETE", f"/api/students/{attacker}", {405}, me),
        Probe("path_traversal", "GET", "/api/students/..%2f..%2fetc%2fpasswd", {404, 422}, me),
        Probe(
            "sql_injection_query",
            "GET",
            "/api/sections?term=' OR 1=1 --",
            {400, 404, 422},
            me,
            empty_ok=True,
        ),
    ]


def _alg_none() -> str:
    import base64

    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc({'sub': 'attacker', 'role': 'admin'})}."


async def flood(base_url: str, token: str, student_id: int, rate: float, seconds: float) -> dict:
    """One principal sends ``rate`` requests per second (open loop) for ``seconds``."""
    statuses: Counter[str] = Counter()
    latencies: list[float] = []
    headers = bearer(token) if token else {}

    async with httpx.AsyncClient(
        base_url=base_url, timeout=10, limits=httpx.Limits(max_connections=200)
    ) as client:

        async def one() -> None:
            started = time.perf_counter()
            try:
                response = await client.get(f"/api/students/{student_id}", headers=headers)
                statuses[str(response.status_code)] += 1
            except httpx.HTTPError as exc:
                statuses[type(exc).__name__] += 1
            latencies.append((time.perf_counter() - started) * 1000)

        tasks = []
        start = time.monotonic()
        total = int(rate * seconds)
        for i in range(total):
            await asyncio.sleep(max(start + i / rate - time.monotonic(), 0))
            tasks.append(asyncio.create_task(one()))
        await asyncio.gather(*tasks)
    return {
        "sent": total,
        "rate": rate,
        "seconds": seconds,
        "statuses": dict(statuses),
        "latency_ms": {q: percentile(latencies, int(q[1:])) for q in ("p50", "p95")},
    }


async def guess_passwords(
    base_url: str,
    usernames: list[str],
    attempts: int,
    rate: float,
    password: str = "wrong-password",
) -> dict:
    """Wrong passwords against the given accounts, round robin, at ``rate`` per second."""
    statuses: Counter[str] = Counter()
    first_block = None
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        start = time.monotonic()
        for i in range(attempts):
            await asyncio.sleep(max(start + i / rate - time.monotonic(), 0))
            user = usernames[i % len(usernames)]
            response = await client.post(
                "/api/auth/login", json={"username": user, "password": password}
            )
            statuses[str(response.status_code)] += 1
            if response.status_code == 429 and first_block is None:
                first_block = i + 1
    return {
        "attempts": attempts,
        "accounts": len(usernames),
        "statuses": dict(statuses),
        "first_block_at_attempt": first_block,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["login"])
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--users", required=True, help="comma separated")
    parser.add_argument("--attempts", type=int, required=True)
    parser.add_argument("--rate", type=float, default=20)
    args = parser.parse_args()
    result = asyncio.run(
        guess_passwords(args.gateway, args.users.split(","), args.attempts, args.rate)
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()


# Runs inside a service container with that service's own database credentials, like code
# that has been taken over: every statement is tried inside a transaction that is rolled back.
PRIVILEGE_SCRIPT = r"""
import asyncio, json, os
import asyncpg

STATEMENTS = {
    "own_schema_read": "SELECT count(*) FROM {own}",
    "other_schema_read": "SELECT count(*) FROM {other}",
    "credentials_read": "SELECT count(*) FROM auth.users",
    "audit_read": "SELECT count(*) FROM audit.events",
    "audit_append": "INSERT INTO audit.events (service, action, outcome) VALUES ('p', 'p', 'ok')",
    "audit_tamper_update": "UPDATE audit.events SET outcome = 'ok'",
    "audit_tamper_delete": "DELETE FROM audit.events",
    "ddl_create": "CREATE TABLE {schema}.probe_table (i int)",
    "ddl_drop": "DROP TABLE {own}",
}


async def main():
    url = os.environ["UFT_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    schema = os.environ["UFT_SERVICE_NAME"]
    own = {"payment": "payment.payments", "student": "student.students"}.get(schema, schema + ".x")
    other = "student.students" if schema == "payment" else "payment.payments"
    conn = await asyncpg.connect(url)
    out = {"role": await conn.fetchval("SELECT current_user")}
    for name, sql in STATEMENTS.items():
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(sql.format(own=own, schema=schema, other=other))
            out[name] = "allowed"
        except asyncpg.InsufficientPrivilegeError:
            out[name] = "denied"
        except Exception as exc:
            out[name] = "error:" + type(exc).__name__
        finally:
            await tx.rollback()
    print(json.dumps(out))


asyncio.run(main())
"""
