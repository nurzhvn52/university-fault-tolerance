"""Open-loop load generator.

Requests start on a fixed schedule given by a rate profile, whatever the response times
are, so a slow or failing system does not lower the offered load (a closed-loop client
would wait and hide the problem). Every attempt becomes one row with its send time on the
shared Docker clock.

The whole plan (send time and request) is built up front from the seeded workload and split
between worker processes. One event loop cannot keep a 300 requests/s schedule while
thousands of requests to a collapsing system wait for their timeouts.
"""

import asyncio
import csv
import gzip
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import astuple, dataclass, fields
from pathlib import Path

import httpx

from lab.workload import Request, Workload

RateProfile = list[tuple[float, float]]  # (duration in seconds, requests per second)
Plan = list[tuple[float, Request]]  # (offset in seconds from the start, request)


@dataclass
class Attempt:
    t: float
    op: str
    service: str
    method: str
    status: int
    error: str
    latency_ms: float
    attempt: int
    intent: str
    instance: str


def is_success(status: int) -> bool:
    """The system answered the request. Business rejections (4xx) are answers; server
    errors, 429 (shed load) and no answer at all are failures."""
    return 0 < status < 500 and status != 429


def schedule(profile: RateProfile) -> list[float]:
    """Offsets in seconds from the start at which requests are started."""
    offsets: list[float] = []
    start = 0.0
    for duration, rate in profile:
        offsets.extend(start + i / rate for i in range(int(duration * rate)))
        start += duration
    return offsets


def make_plan(profile: RateProfile, workload: Workload) -> Plan:
    return [(offset, workload.next()) for offset in schedule(profile)]


class LoadGenerator:
    """Runs one part of a plan in the current event loop."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        timeout_s: float = 5.0,
        retry_delay_s: float = 1.0,
        max_in_flight: int = 500,
    ) -> None:
        self._http = http
        self._timeout_s = timeout_s
        self._retry_delay_s = retry_delay_s
        self._max_in_flight = max_in_flight
        self.attempts: list[Attempt] = []

    async def run(self, plan: Plan, start_mono: float) -> None:
        in_flight: set[asyncio.Task] = set()
        for offset, request in plan:
            delay = start_mono + offset - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if len(in_flight) >= self._max_in_flight:
                self._record(request, time.time(), 0, "dropped", 0.0, 1, "")
                continue
            task = asyncio.create_task(self._issue(request))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
        if in_flight:
            await asyncio.gather(*in_flight)

    async def _issue(self, request: Request) -> None:
        for attempt in range(1, request.retries + 2):
            status = await self._send(request, attempt)
            if is_success(status) or attempt > request.retries:
                return
            await asyncio.sleep(self._retry_delay_s)

    async def _send(self, request: Request, attempt: int) -> int:
        sent_at, started = time.time(), time.perf_counter()
        status, error, instance = 0, "", ""
        try:
            async with asyncio.timeout(self._timeout_s):
                response = await self._http.request(
                    request.method,
                    request.path,
                    params=request.params,
                    json=request.json,
                    headers=request.headers,
                )
            status = response.status_code
            instance = response.headers.get("X-Instance", "")
        except (TimeoutError, httpx.TimeoutException):
            error = "timeout"
        except httpx.ConnectError:
            error = "connect"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        latency_ms = (time.perf_counter() - started) * 1000
        self._record(request, sent_at, status, error, latency_ms, attempt, instance)
        return status

    def _record(self, request, sent_at, status, error, latency_ms, attempt, instance) -> None:
        self.attempts.append(
            Attempt(
                t=sent_at,
                op=request.op,
                service=request.service,
                method=request.method,
                status=status,
                error=error,
                latency_ms=round(latency_ms, 2),
                attempt=attempt,
                intent=request.intent,
                instance=instance,
            )
        )


def _warm_up() -> None:
    """Runs in each worker before the start, so that process start-up is not measured."""


def _run_part(base_url: str, plan: Plan, start_mono: float, max_in_flight: int) -> list:
    async def main() -> list[Attempt]:
        limits = httpx.Limits(max_connections=500, max_keepalive_connections=100)
        async with httpx.AsyncClient(base_url=base_url, limits=limits) as http:
            generator = LoadGenerator(http, max_in_flight=max_in_flight)
            await generator.run(plan, start_mono)
            return generator.attempts

    return asyncio.run(main())


class DistributedLoad:
    """A pool of worker processes; every worker takes every n-th request of the plan.

    time.monotonic() is the system-wide monotonic clock on Linux, so the start instant is
    the same in all processes.
    """

    def __init__(self, base_url: str, workers: int = 4, max_in_flight: int = 2000) -> None:
        self._base_url = base_url
        self._workers = workers
        self._max_in_flight = max_in_flight
        self._pool = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            *(loop.run_in_executor(self._pool, _warm_up) for _ in range(self._workers))
        )

    async def run(self, plan: Plan, start_mono: float) -> list[Attempt]:
        loop = asyncio.get_running_loop()
        parts = await asyncio.gather(
            *(
                loop.run_in_executor(
                    self._pool,
                    _run_part,
                    self._base_url,
                    plan[i :: self._workers],
                    start_mono,
                    self._max_in_flight // self._workers,
                )
                for i in range(self._workers)
            )
        )
        return [attempt for part in parts for attempt in part]

    def close(self) -> None:
        self._pool.shutdown()


FIELDS = [f.name for f in fields(Attempt)]


def write_attempts(attempts: list[Attempt], path: Path) -> None:
    with gzip.open(path, "wt", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(FIELDS)
        writer.writerows(astuple(a) for a in sorted(attempts, key=lambda a: a.t))


def read_attempts(path: Path) -> list[Attempt]:
    with gzip.open(path, "rt", newline="") as file:
        return [
            Attempt(
                t=float(row["t"]),
                op=row["op"],
                service=row["service"],
                method=row["method"],
                status=int(row["status"]),
                error=row["error"],
                latency_ms=float(row["latency_ms"]),
                attempt=int(row["attempt"]),
                intent=row["intent"],
                instance=row["instance"],
            )
            for row in csv.DictReader(file)
        ]
