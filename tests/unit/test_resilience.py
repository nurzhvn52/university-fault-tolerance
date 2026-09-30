import asyncio
import random

import httpx
import pytest

from common.resilience import (
    Bulkhead,
    CircuitBreaker,
    CircuitOpen,
    RateLimitedLog,
    backoff_delays,
    is_transient,
    retry,
)


def run(coro):
    return asyncio.run(coro)


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://x")
    return httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(code, request=request)
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Flaky:
    """Fails with the given exceptions first, then returns "ok"."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def test_transient_failures_are_retryable_business_errors_are_not():
    assert is_transient(TimeoutError())
    assert is_transient(httpx.ConnectError("refused"))
    assert is_transient(status_error(503))
    assert not is_transient(status_error(409))
    assert not is_transient(CircuitOpen("bank"))
    assert not is_transient(ValueError())


def test_backoff_grows_exponentially_up_to_the_cap_with_jitter():
    delays = backoff_delays(6, base=0.1, cap=1.0, rng=random.Random(1))

    assert len(delays) == 5
    for n, delay in enumerate(delays):
        assert 0 <= delay <= min(1.0, 0.1 * 2**n)


def test_retry_recovers_from_transient_failures():
    call = Flaky(TimeoutError(), httpx.ConnectError("x"))
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    result = run(retry(call, name="dep", attempts=3, base_delay=0.1, max_delay=1, sleep=fake_sleep))

    assert result == "ok"
    assert call.calls == 3
    assert len(waits) == 2


def test_retry_gives_up_after_the_last_attempt():
    call = Flaky(TimeoutError(), TimeoutError(), TimeoutError())

    async def no_sleep(_):
        pass

    with pytest.raises(TimeoutError):
        run(retry(call, name="dep", attempts=3, base_delay=0.1, max_delay=1, sleep=no_sleep))
    assert call.calls == 3


def test_retry_does_not_repeat_non_transient_errors():
    call = Flaky(status_error(409))

    with pytest.raises(httpx.HTTPStatusError):
        run(retry(call, name="dep", attempts=3, base_delay=0, max_delay=0))
    assert call.calls == 1


def test_breaker_opens_after_consecutive_failures_and_fails_fast():
    clock = Clock()
    breaker = CircuitBreaker("dep", failures=3, reset_s=5, clock=clock)
    failing = Flaky(*[TimeoutError()] * 10)

    for _ in range(3):
        with pytest.raises(TimeoutError):
            run(breaker.call(failing))
    assert breaker.state == "open"

    with pytest.raises(CircuitOpen):
        run(breaker.call(failing))
    assert failing.calls == 3


def test_breaker_half_open_trial_closes_or_reopens():
    clock = Clock()
    breaker = CircuitBreaker("dep", failures=1, reset_s=5, clock=clock)
    with pytest.raises(TimeoutError):
        run(breaker.call(Flaky(TimeoutError())))

    clock.now = 6
    with pytest.raises(TimeoutError):
        run(breaker.call(Flaky(TimeoutError())))
    assert breaker.state == "open"

    clock.now = 12
    assert run(breaker.call(Flaky())) == "ok"
    assert breaker.state == "closed"


def test_breaker_lets_only_one_trial_call_through():
    clock = Clock()
    breaker = CircuitBreaker("dep", failures=1, reset_s=5, clock=clock)
    with pytest.raises(TimeoutError):
        run(breaker.call(Flaky(TimeoutError())))
    clock.now = 6

    async def two_calls():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return "ok"

        first = asyncio.create_task(breaker.call(slow))
        await asyncio.sleep(0)
        with pytest.raises(CircuitOpen):
            await breaker.call(slow)
        gate.set()
        return await first

    assert run(two_calls()) == "ok"
    assert breaker.state == "closed"


def test_business_errors_do_not_open_the_breaker():
    breaker = CircuitBreaker("dep", failures=1, reset_s=5)

    with pytest.raises(httpx.HTTPStatusError):
        run(breaker.call(Flaky(status_error(404))))
    assert breaker.state == "closed"


def test_bulkhead_rejects_calls_over_the_limit():
    bulkhead = Bulkhead("in", limit=2)

    assert bulkhead.try_acquire() and bulkhead.try_acquire()
    assert not bulkhead.try_acquire()
    bulkhead.release()
    assert bulkhead.try_acquire()


def test_rate_limited_log():
    clock = Clock()
    limiter = RateLimitedLog(every_s=5, clock=clock)

    assert limiter.should_log("db")
    assert not limiter.should_log("db")
    assert limiter.should_log("other")
    clock.now = 6
    assert limiter.should_log("db")


def test_database_errors_are_classified():
    from sqlalchemy import exc as sa_exc

    from common.db import is_db_unavailable

    class PgError(Exception):
        def __init__(self, sqlstate):
            self.sqlstate = sqlstate

    def dbapi(sqlstate):
        return sa_exc.DBAPIError("SELECT 1", {}, PgError(sqlstate))

    assert is_db_unavailable(ConnectionRefusedError())
    assert is_db_unavailable(TimeoutError())
    assert is_db_unavailable(dbapi("57P03"))
    assert is_db_unavailable(dbapi("08006"))
    assert not is_db_unavailable(dbapi("23505"))
    assert not is_db_unavailable(ValueError())
