"""Evidence for the security scenarios, read straight from the database.

*Victims* are the last students of the population; the legitimate load never touches them
(``Workload`` excludes them), so any change to their data during a run was made by an attacker.
The audit report compares what the services logged with what reached ``audit.events``.
"""

import json
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from lab.consistency import _rows

VICTIM_QUERIES = {
    "registrations": (
        "SELECT id, student_id, section_id, status FROM student.registrations "
        "WHERE student_id = ANY(:v) ORDER BY id"
    ),
    "payments": (
        "SELECT id, student_id, amount, status FROM payment.payments "
        "WHERE student_id = ANY(:v) ORDER BY id"
    ),
    "invoices": (
        "SELECT student_id, amount_paid FROM payment.invoices WHERE student_id = ANY(:v) "
        "ORDER BY student_id, term"
    ),
    "grades": (
        "SELECT id, student_id, course_code, term, letter FROM records.grades "
        "WHERE student_id = ANY(:v) ORDER BY id"
    ),
    "documents": (
        "SELECT id, student_id, sha256 FROM records.transcript_documents "
        "WHERE student_id = ANY(:v) ORDER BY id"
    ),
}


async def victim_ids(engine: AsyncEngine, count: int) -> list[int]:
    rows = await _rows(engine, "SELECT id FROM student.students ORDER BY id DESC LIMIT :n", n=count)
    return sorted(r["id"] for r in rows)


async def victim_markers(engine: AsyncEngine, victims: list[int]) -> list[str]:
    rows = await _rows(
        engine,
        "SELECT full_name, student_no FROM student.students WHERE id = ANY(:v)",
        v=victims,
    )
    return [value for row in rows for value in (row["full_name"], row["student_no"])]


async def snapshot(engine: AsyncEngine, victims: list[int]) -> dict:
    state = {name: await _rows(engine, sql, v=victims) for name, sql in VICTIM_QUERIES.items()}
    state["poisoned_grades"] = await _rows(
        engine, "SELECT id FROM records.grades WHERE term = '2026-HACK'"
    )
    return json.loads(json.dumps(state, default=str))


def diff(before: dict, after: dict) -> dict:
    """Violations per entity: rows of victims added, removed or changed, and poisoned grades."""
    report = {}
    for name in VICTIM_QUERIES:
        changed = sum(1 for row in after[name] if row not in before[name]) + sum(
            1 for row in before[name] if row not in after[name]
        )
        report[f"victim_{name}_changed"] = {"violations": changed}
    report["poisoned_grades"] = {"violations": len(after["poisoned_grades"])}
    return report


async def audit_rows(engine: AsyncEngine, since: float) -> list[dict]:
    stamp = datetime.fromtimestamp(since).astimezone()
    return await _rows(
        engine,
        "SELECT service, request_id, actor, action, outcome, resource, detail "
        "FROM audit.events WHERE ts >= :since",
        since=stamp,
    )


def audit_report(rows: list[dict], logs: list[dict], reasons: list[str]) -> dict:
    """``logged``: audit events the services wrote to their logs; ``stored``: rows in the
    table. Events still in a buffer when a service died are lost from the table, not the log.
    ``reasons``: denial reasons seen by the attacker, each should have an event."""
    logged = sum(
        1
        for e in logs
        if e.get("json", {}).get("logger") == "audit" and e["json"].get("event") == "audit"
    )
    stored_reasons = {
        (r["detail"] or {}).get("reason")
        for r in rows
        if r["action"] in ("auth.denied", "rate_limit", "request.rejected")
    }
    missing = [r for r in reasons if r not in stored_reasons]
    by_action: dict[str, int] = {}
    for r in rows:
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1
    return {
        "logged": logged,
        "stored": len(rows),
        "lost_from_table": {"violations": max(logged - len(rows), 0)},
        "denial_reasons_without_event": {"violations": len(missing), "reasons": missing},
        "by_action": by_action,
    }


def summarize_actions(records: list[dict]) -> dict:
    """Condenses the results of the misuse actions of a run for the metrics file."""
    out: dict = {}
    for record in records:
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        name = record["do"]
        if name in ("probe_unauthorized", "probe_malformed"):
            out[name] = {
                k: result[k]
                for k in ("total", "refused", "succeeded", "leaked", "server_errors", "statuses")
            }
        elif name == "flood":
            out.setdefault("floods", []).append(result)
        elif name == "login_attack":
            out.setdefault("login_attacks", []).append(result)
        elif name == "legit_login":
            out.setdefault("legit_logins", []).append(result)
        elif name == "privilege_probe":
            out.setdefault("privilege", []).append(result)
    return out
