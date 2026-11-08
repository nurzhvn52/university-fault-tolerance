"""Token bucket rate limiter keyed by principal.

State is in memory, per instance: with two replicas behind a round-robin gateway a client can
get up to twice the configured rate. Idle keys are dropped so the table stays bounded.
"""

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10000,
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill)

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        if len(self._buckets) >= self._max_keys and key not in self._buckets:
            self._prune(now)
        self._buckets[key] = (tokens, now)
        return allowed

    def _prune(self, now: float) -> None:
        # A bucket idle long enough to be full again carries no information.
        full_after = self._burst / self._rate if self._rate > 0 else 0.0
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}
