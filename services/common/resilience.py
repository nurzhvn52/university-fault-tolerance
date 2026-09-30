"""Software fault-tolerance building blocks: retry with backoff, circuit breaker, bulkhead.

Composition used by the services for a call to a dependency:

    retry( breaker( timeout( call ) ) )

every attempt has its own timeout, the breaker sees every attempt and, once open, makes the
remaining attempts fail at once instead of waiting on a dependency that is down.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

logger = logging.getLogger("common.resilience")

T = TypeVar("T")


class DependencyUnavailable(Exception):
    """A dependency could not be used; the caller should degrade or answer 503."""


class CircuitOpen(DependencyUnavailable):
    pass


class BulkheadFull(DependencyUnavailable):
    pass


def is_transient(exc: BaseException) -> bool:
    """Failures worth retrying: no answer, a timeout, or a 5xx / 429 answer."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, TimeoutError | httpx.TransportError | DependencyUnavailable) and (
        not isinstance(exc, CircuitOpen)
    )


def backoff_delays(attempts: int, base: float, cap: float, rng: random.Random) -> list[float]:
    """Full jitter: the n-th wait is uniform in [0, min(cap, base * 2**n)]."""
    return [rng.uniform(0, min(cap, base * 2**n)) for n in range(attempts - 1)]


async def retry(
    call: Callable[[], Awaitable[T]],
    *,
    name: str,
    attempts: int,
    base_delay: float,
    max_delay: float,
    retry_on: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    """Only for idempotent operations: a retried call may have taken effect before."""
    delays = backoff_delays(attempts, base_delay, max_delay, rng or random.Random())
    for attempt in range(1, attempts + 1):
        try:
            result = await call()
        except Exception as exc:
            if attempt == attempts or not retry_on(exc):
                raise
            delay = delays[attempt - 1]
            logger.info(
                "retry_attempt",
                extra={"dependency": name, "attempt": attempt, "error": type(exc).__name__},
            )
            await sleep(delay)
            continue
        if attempt > 1:
            logger.info("retry_succeeded", extra={"dependency": name, "attempts": attempt})
        return result
    raise AssertionError("unreachable")


class CircuitBreaker:
    """Closed -> open after ``failures`` consecutive failures; after ``reset_s`` one trial call
    is let through (half-open); its success closes the breaker, its failure opens it again.

    Not locked: all state changes happen between awaits of one event loop.
    """

    def __init__(
        self,
        name: str,
        *,
        failures: int,
        reset_s: float,
        clock: Callable[[], float] = time.monotonic,
        counts_as_failure: Callable[[BaseException], bool] = is_transient,
    ) -> None:
        self.name = name
        self._threshold = failures
        self._reset_s = reset_s
        self._clock = clock
        self._counts = counts_as_failure
        self.state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trial_running = False

    async def call(self, call: Callable[[], Awaitable[T]]) -> T:
        if self.state == "open":
            if self._clock() - self._opened_at < self._reset_s:
                raise CircuitOpen(self.name)
            self._set("half_open")
        if self.state == "half_open":
            if self._trial_running:
                raise CircuitOpen(self.name)
            self._trial_running = True
        try:
            result = await call()
        except Exception as exc:
            if self._counts(exc):
                self._on_failure()
            elif self.state == "half_open":
                self._on_success()
            raise
        finally:
            self._trial_running = False
        self._on_success()
        return result

    def _on_success(self) -> None:
        self._failures = 0
        if self.state != "closed":
            self._set("closed")

    def _on_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self._threshold:
            self._opened_at = self._clock()
            self._set("open")

    def _set(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        level = logging.WARNING if state == "open" else logging.INFO
        event = {"open": "breaker_opened", "half_open": "breaker_half_open"}.get(
            state, "breaker_closed"
        )
        logger.log(level, event, extra={"dependency": self.name})


class Bulkhead:
    """Limits how many calls run at once; extra calls are rejected immediately, so one slow
    dependency or a traffic spike cannot take all the resources of the process."""

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.limit = limit
        self.in_use = 0

    def try_acquire(self) -> bool:
        if self.in_use >= self.limit:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        self.in_use -= 1


class RateLimitedLog:
    """Logs an event at most once per ``every_s`` per key (e.g. db_unavailable under load)."""

    def __init__(self, every_s: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self._every_s = every_s
        self._clock = clock
        self._last: dict[str, float] = {}

    def should_log(self, key: str) -> bool:
        now = self._clock()
        if now - self._last.get(key, -1e9) < self._every_s:
            return False
        self._last[key] = now
        return True


class Dependency:
    """A remote dependency called with a timeout per attempt, a circuit breaker and, for
    idempotent calls, retries with backoff. A call that cannot be completed raises
    ``DependencyUnavailable``, which the caller turns into a fallback or a 503."""

    def __init__(
        self,
        name: str,
        *,
        timeout_s: float,
        attempts: int,
        base_delay_s: float,
        max_delay_s: float,
        breaker_failures: int,
        breaker_reset_s: float,
    ) -> None:
        self.name = name
        self.timeout_s = timeout_s
        self.attempts = attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.breaker = CircuitBreaker(name, failures=breaker_failures, reset_s=breaker_reset_s)

    @classmethod
    def from_settings(
        cls, name: str, settings, timeout_s: float, attempts: int | None = None
    ) -> "Dependency":
        return cls(
            name,
            timeout_s=timeout_s,
            attempts=attempts or settings.retry_attempts,
            base_delay_s=settings.retry_base_delay_s,
            max_delay_s=settings.retry_max_delay_s,
            breaker_failures=settings.breaker_failures,
            breaker_reset_s=settings.breaker_reset_s,
        )

    async def call(self, call: Callable[[], Awaitable[T]], *, idempotent: bool = True) -> T:
        async def attempt() -> T:
            async with asyncio.timeout(self.timeout_s):
                return await call()

        async def guarded() -> T:
            return await self.breaker.call(attempt)

        try:
            if not idempotent:
                return await guarded()
            return await retry(
                guarded,
                name=self.name,
                attempts=self.attempts,
                base_delay=self.base_delay_s,
                max_delay=self.max_delay_s,
            )
        except CircuitOpen:
            raise
        except Exception as exc:
            if is_transient(exc):
                raise DependencyUnavailable(self.name) from exc
            raise
