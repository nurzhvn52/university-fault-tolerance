"""Prometheus metrics of a service instance (scraped at /metrics, not routed by the gateway)."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "uft_requests_total", "HTTP requests handled", ["service", "route", "method", "status"]
)
LATENCY = Histogram(
    "uft_request_seconds",
    "HTTP request duration",
    ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
IN_FLIGHT = Gauge("uft_in_flight_requests", "Requests being handled", ["service"])
BREAKER_OPEN = Gauge(
    "uft_breaker_open", "1 while the circuit breaker of a dependency is open", ["dependency"]
)
SHED = Counter("uft_load_shed_total", "Requests rejected by load shedding", ["service"])

DEPENDENCY_CALLS = Counter(
    "uft_dependency_calls_total",
    "Attempts of calls to a dependency by outcome (ok, timeout, error, circuit_open)",
    ["dependency", "outcome"],
)
DEPENDENCY_SECONDS = Histogram(
    "uft_dependency_seconds",
    "Duration of one attempt of a call to a dependency",
    ["dependency"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
RETRIES = Counter("uft_retries_total", "Retried attempts", ["dependency"])
BUILD_INFO = Gauge(
    "uft_build_info", "Version and feature flags of the instance", ["service", "version", "mode"]
)

AUTH_DENIED = Counter(
    "uft_auth_denied_total",
    "Requests rejected by authentication or authorization",
    ["service", "reason"],
)
RATE_LIMITED = Counter(
    "uft_rate_limited_total", "Requests rejected by rate limiting", ["service", "scope"]
)
REJECTED = Counter(
    "uft_rejected_requests_total",
    "Requests rejected before reaching a handler",
    ["service", "reason"],
)
AUDIT_EVENTS = Counter(
    "uft_audit_events_total", "Audit events recorded", ["service", "action", "outcome"]
)
AUDIT_DROPPED = Counter(
    "uft_audit_dropped_total", "Audit events lost before reaching the database", ["service"]
)
