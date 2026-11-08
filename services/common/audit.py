"""Audit trail: who did what, with which outcome, tied to the request id.

Every event goes to the JSON log at once (the authoritative stream, it survives a database
outage) and to a bounded in-memory buffer that a background task writes to ``audit.events``
in batches. The business operation never waits for the audit write and never fails because of
it: if the database is down the events stay in the buffer and are written when it is back;
if the buffer is full the oldest events are dropped and counted
(``uft_audit_dropped_total``).

Repeated security events with the same key (the same actor probing the same resource) are
logged once per window with a count of the suppressed repeats, so a flood does not drown the
trail; the metric still counts every one.
"""

import asyncio
import contextlib
import logging
import socket
import time
from collections import deque

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, func, insert
from sqlalchemy.ext.asyncio import AsyncEngine

from common.context import current_request_id
from common.resilience import RateLimitedLog
from common.telemetry import AUDIT_DROPPED, AUDIT_EVENTS

logger = logging.getLogger("audit")

metadata = MetaData(schema="audit")
events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime(timezone=True), server_default=func.now()),
    Column("service", String(32)),
    Column("instance", String(64)),
    Column("request_id", String(64)),
    Column("actor", String(128)),
    Column("action", String(48)),
    Column("outcome", String(16)),
    Column("resource", String(160)),
    Column("detail", JSON),
)

BATCH = 100
DEDUP_WINDOW_S = 5.0


class AuditLog:
    def __init__(self, service: str, engine: AsyncEngine | None, *, queue_size: int = 5000) -> None:
        self._service = service
        self._instance = socket.gethostname()
        self._engine = engine
        self._buffer: deque[dict] = deque(maxlen=queue_size)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._seen: dict[tuple, tuple[float, int]] = {}
        self._warn = RateLimitedLog(5.0)
        self.written = 0

    def record(
        self,
        action: str,
        outcome: str,
        *,
        actor: str | None = None,
        resource: str | None = None,
        detail: dict | None = None,
        dedup: tuple | None = None,
    ) -> None:
        AUDIT_EVENTS.labels(self._service, action, outcome).inc()
        detail = dict(detail or {})
        if dedup is not None:
            now = time.monotonic()
            last, suppressed = self._seen.get(dedup, (-1e9, 0))
            if now - last < DEDUP_WINDOW_S:
                self._seen[dedup] = (last, suppressed + 1)
                return
            if suppressed:
                detail["suppressed_repeats"] = suppressed
            self._seen[dedup] = (now, 0)
            if len(self._seen) > 5000:
                self._seen = {k: v for k, v in self._seen.items() if now - v[0] < DEDUP_WINDOW_S}
        entry = {
            "service": self._service,
            "instance": self._instance,
            "request_id": current_request_id(),
            "actor": actor,
            "action": action,
            "outcome": outcome,
            "resource": resource,
            "detail": detail,
        }
        logger.info(
            "audit", extra={k: v for k, v in entry.items() if k not in ("service", "instance")}
        )
        if self._engine is None:
            return
        if len(self._buffer) == self._buffer.maxlen:
            AUDIT_DROPPED.labels(self._service).inc()
        self._buffer.append(entry)
        self._wake.set()

    def start(self) -> None:
        if self._engine is not None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            await self._flush_once(final=True)

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._buffer:
                if not await self._flush_once():
                    await asyncio.sleep(1.0)

    async def _flush_once(self, *, final: bool = False) -> bool:
        batch = [self._buffer.popleft() for _ in range(min(BATCH, len(self._buffer)))]
        if not batch:
            return True
        try:
            async with asyncio.timeout(3.0), self._engine.begin() as conn:
                await conn.execute(insert(events), batch)
        except Exception as exc:
            if not final:
                # Keep the events for the next attempt; the bounded buffer drops the oldest.
                for entry in reversed(batch):
                    if len(self._buffer) < self._buffer.maxlen:
                        self._buffer.appendleft(entry)
                    else:
                        AUDIT_DROPPED.labels(self._service).inc()
            else:
                AUDIT_DROPPED.labels(self._service).inc(len(batch))
            if self._warn.should_log("audit_db"):
                logger.warning(
                    "audit_db_unavailable",
                    extra={"error": type(exc).__name__, "buffered": len(self._buffer)},
                )
            return False
        self.written += len(batch)
        return True


class NullAudit:
    """Audit off (midterm configuration)."""

    def record(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass
