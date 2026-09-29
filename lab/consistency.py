"""Data consistency checks, run after every experiment.

Every check reports ``violations``; a consistent system has 0 everywhere. The checks read
the service schemas and the bank directly, bypassing the services under test.
"""

import hashlib
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from lab.actions import GRADE_BATCH_TERM


def check_payments(payments: list[dict], charges: list[dict], invoices: list[dict]) -> dict:
    recorded = {p["bank_charge_id"] for p in payments if p["bank_charge_id"]}
    charge_ids = {c["charge_id"] for c in charges}
    # Money taken by the bank with no payment record.
    lost = [c for c in charges if c["charge_id"] not in recorded]
    # Captured payments the bank knows nothing about.
    phantom = [
        p for p in payments if p["status"] == "CAPTURED" and p["bank_charge_id"] not in charge_ids
    ]
    # Every client payment has its own amount, so equal (account, amount) means double charge.
    per_payment = Counter((c["account"], Decimal(c["amount"])) for c in charges)
    captured: defaultdict[tuple, Decimal] = defaultdict(Decimal)
    for p in payments:
        if p["status"] == "CAPTURED":
            captured[(p["student_id"], p["term"])] += Decimal(p["amount"])
    mismatched = [
        i
        for i in invoices
        if Decimal(i["amount_paid"])
        != Decimal(i["opening_paid"]) + captured[(i["student_id"], i["term"])]
    ]
    return {
        "bank_charges": len(charges),
        "payment_records": len(payments),
        "lost_payments": {
            "violations": len(lost),
            "amount": str(sum((Decimal(c["amount"]) for c in lost), Decimal(0))),
        },
        "duplicate_charges": {
            "violations": sum(n - 1 for n in per_payment.values() if n > 1),
        },
        "phantom_payments": {"violations": len(phantom)},
        "invoice_mismatch": {"violations": len(mismatched)},
        "unfinished_payments": {
            "violations": sum(p["status"] not in ("CAPTURED", "FAILED") for p in payments)
        },
    }


def check_sections(sections: list[dict]) -> dict:
    """sections: {id, capacity, enrolled, registered}; registered is the real row count."""
    overbooked = [s for s in sections if s["registered"] > s["capacity"]]
    return {
        "overbooked_sections": {
            "violations": len(overbooked),
            "extra_seats": sum(s["registered"] - s["capacity"] for s in overbooked),
        },
        "seat_counter_drift": {
            "violations": sum(s["enrolled"] != s["registered"] for s in sections),
            "total_abs_drift": sum(abs(s["enrolled"] - s["registered"]) for s in sections),
        },
    }


def check_documents(documents: list[dict], roots: list[Path]) -> dict:
    """documents: {id, storage_key, sha256}. A document is lost if no copy has the right hash."""
    bad_copies = 0
    lost = 0
    for document in documents:
        good = 0
        for root in roots:
            path = root / document["storage_key"]
            ok = (
                path.is_file()
                and hashlib.sha256(path.read_bytes()).hexdigest() == document["sha256"]
            )
            good += ok
            bad_copies += not ok
        lost += good == 0
    return {
        "documents": len(documents),
        "lost_documents": {"violations": lost},
        "bad_copies": {"violations": bad_copies},
    }


def check_jobs(jobs: list[dict], entries: int, sections: int) -> dict:
    return {
        "stuck_jobs": {"violations": sum(job["status"] == "RUNNING" for job in jobs)},
        "unscheduled_sections": {"violations": max(sections - entries, 0)},
    }


def check_grade_batch(imported: int, expected: int | None) -> dict:
    """A batch import is consistent when it is either complete or absent."""
    partial = expected is not None and 0 < imported < expected
    return {
        "imported": imported,
        "expected": expected,
        "partial_batch": {"violations": int(partial)},
    }


def violations(report: dict) -> dict[str, int]:
    """Flatten a report to {check name: violations}."""
    flat = {}
    for group in report.values():
        if isinstance(group, dict):
            for name, value in group.items():
                if isinstance(value, dict) and "violations" in value:
                    flat[name] = value["violations"]
    return flat


async def _rows(engine: AsyncEngine, sql: str, **params) -> list[dict]:
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return [dict(row._mapping) for row in result]


async def run_checks(
    engine: AsyncEngine,
    http: httpx.AsyncClient,
    bank_url: str,
    transcript_roots: list[Path],
    expected_batch: int | None,
) -> dict:
    charges_response = await http.get(f"{bank_url}/charges", timeout=30)
    charges_response.raise_for_status()
    payments = await _rows(
        engine, "SELECT id, student_id, term, amount, status, bank_charge_id FROM payment.payments"
    )
    invoices = await _rows(
        engine, "SELECT student_id, term, opening_paid, amount_paid FROM payment.invoices"
    )
    sections = await _rows(
        engine,
        """
        SELECT s.id, s.capacity, s.enrolled, count(r.id) AS registered
        FROM student.sections s LEFT JOIN student.registrations r ON r.section_id = s.id
        GROUP BY s.id
        """,
    )
    unpaid = await _rows(
        engine,
        """
        SELECT count(*) AS n
        FROM student.registrations r
        JOIN student.sections s ON s.id = r.section_id
        JOIN payment.invoices i ON i.student_id = r.student_id AND i.term = s.term
        WHERE r.status = 'CONFIRMED' AND i.amount_paid < i.amount_due
        """,
    )
    documents = await _rows(
        engine, "SELECT id, storage_key, sha256 FROM records.transcript_documents"
    )
    jobs = await _rows(engine, "SELECT id, status FROM timetable.generation_jobs")
    entries = await _rows(engine, "SELECT count(*) AS n FROM timetable.entries")
    batch = await _rows(
        engine, "SELECT count(*) AS n FROM records.grades WHERE term = :term", term=GRADE_BATCH_TERM
    )
    return {
        "payments": check_payments(payments, charges_response.json(), invoices),
        "registrations": {
            **check_sections(sections),
            "confirmed_without_tuition": {"violations": unpaid[0]["n"]},
        },
        "records": {
            **check_documents(documents, transcript_roots),
            **check_grade_batch(batch[0]["n"], expected_batch),
        },
        "timetable": check_jobs(jobs, entries[0]["n"], len(sections)),
    }
