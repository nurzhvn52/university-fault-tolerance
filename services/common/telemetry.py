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
