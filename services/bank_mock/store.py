"""Charge storage of the simulated bank (SQLite, separate from the university database)."""

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
    charge_id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    amount TEXT NOT NULL,
    reference TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
_COLUMNS = "charge_id, account, amount, reference, idempotency_key, status, created_at"


class ChargeStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._lock = threading.Lock()

    def create(
        self, account: str, amount: Decimal, reference: str, idempotency_key: str | None
    ) -> tuple[dict, bool]:
        """Store a charge. Returns (charge, created); a known idempotency key returns the
        original charge with created=False instead of charging again."""
        with self._lock:
            if idempotency_key is not None:
                existing = self._one("idempotency_key = ?", idempotency_key)
                if existing is not None:
                    return existing, False
            charge = {
                "charge_id": f"ch_{uuid.uuid4().hex[:16]}",
                "account": account,
                "amount": str(amount),
                "reference": reference,
                "idempotency_key": idempotency_key,
                "status": "SUCCEEDED",
                "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            }
            self._conn.execute(
                f"INSERT INTO charges ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(charge.values()),
            )
            return charge, True

    def get(self, charge_id: str) -> dict | None:
        with self._lock:
            return self._one("charge_id = ?", charge_id)

    def find(self, *, reference: str | None = None, idempotency_key: str | None = None) -> list:
        clauses, params = [], []
        if reference is not None:
            clauses.append("reference = ?")
            params.append(reference)
        if idempotency_key is not None:
            clauses.append("idempotency_key = ?")
            params.append(idempotency_key)
        where = " AND ".join(clauses) or "1 = 1"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM charges WHERE {where} ORDER BY created_at", params
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict:
        with self._lock:
            amounts = [row[0] for row in self._conn.execute("SELECT amount FROM charges")]
        return {"count": len(amounts), "total_amount": str(sum(map(Decimal, amounts), Decimal(0)))}

    def close(self) -> None:
        self._conn.close()

    def _one(self, where: str, value: str) -> dict | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM charges WHERE {where}", (value,)
        ).fetchone()
        return dict(row) if row is not None else None
