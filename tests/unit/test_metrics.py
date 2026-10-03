import pytest

from lab.loadgen import Attempt
from lab.metrics import (
    automatic_restarts,
    buckets,
    detection_signals,
    fault_metrics,
    injected,
    load_steps,
    outages,
    payment_intents,
    percentile,
    reliability,
    request_stats,
    unavailable,
)


def attempt(
    t: float, status: int = 200, *, intent: str = "", n: int = 1, service="student", ms=10.0
):
    return Attempt(t, "op", service, "GET", status, "", ms, n, intent, "x")


def load(seconds: int, failing: set[int], per_second: int = 10) -> list[Attempt]:
    """per_second requests in every second; all fail in the seconds listed in failing."""
    return [
        attempt(s + i / per_second, 500 if s in failing else 200)
        for s in range(seconds)
        for i in range(per_second)
    ]


def test_reliability_matches_the_lecture_example():
    # Week 1: 720 h observed, 4 failures with 4 h of downtime in total.
    spans = [(0, 1), (100, 101), (200, 201), (300, 301)]

    result = reliability(spans, t_obs=720)

    assert result["D_total_s"] == 4
    assert result["U_total_s"] == 716
    assert result["availability"] == pytest.approx(0.99444, abs=1e-5)
    assert result["MTTF_s"] == 179
    assert result["MTTR_s"] == 1
    assert result["MTBF_s"] == 180


def test_no_failures_gives_full_availability():
    result = reliability([], t_obs=100)

    assert result["availability"] == 1
    assert result["MTTF_s"] is None
    assert result["failure_rate_per_h"] == 0


def test_a_second_is_unavailable_below_95_percent():
    good = [attempt(0.01 * i) for i in range(96)] + [attempt(0.99, 500)] * 4
    bad = [attempt(1 + 0.01 * i) for i in range(94)] + [attempt(1.99, 500)] * 6

    assert unavailable(buckets(good + bad, 0, 2)) == [False, True]


def test_empty_seconds_keep_the_previous_state():
    attempts = [attempt(0.5, 500), attempt(2.5)]

    assert unavailable(buckets(attempts, 0, 3)) == [True, True, False]


def test_outages_close_together_are_merged():
    assert outages(buckets(load(20, {3, 4}), 0, 20)) == [(3, 5)]
    assert outages(buckets(load(20, {3, 5}), 0, 20)) == [(3, 6)]
    assert outages(buckets(load(20, {3, 8}), 0, 20)) == [(3, 4), (8, 9)]


def test_fault_metrics_measure_impact_detection_and_recovery():
    attempts = load(40, failing={21, 22, 23, 24})
    signals = [(5.0, "early:ignored"), (21.4, "gateway:haproxy_server_down")]

    result = fault_metrics(attempts, fault_at=20, t_end=40, signals=signals)

    assert result["client_impact_s"] == 1.0
    assert result["detection_s"] == 1.4
    assert result["detection_source"] == "gateway:haproxy_server_down"
    assert result["recovery_s"] == 5.0
    assert result["downtime_s"] == 4.0
    assert result["requests"]["failed"] == 40


def test_masked_failure_has_zero_recovery_time():
    result = fault_metrics(load(30, set()), fault_at=10, t_end=30, signals=[])

    assert result["recovery_s"] == 0
    assert result["client_impact_s"] is None
    assert result["detection_s"] is None


def test_payment_intents_count_client_retries():
    attempts = [
        attempt(1, 201, intent="a"),
        attempt(2, 500, intent="b"),
        attempt(3, 201, intent="b", n=2),
        attempt(4, 0, intent="c"),
        attempt(5, 502, intent="c", n=2),
        attempt(6, 502, intent="c", n=3),
    ]

    assert payment_intents(attempts) == {
        "intents": 3,
        "first_attempt_failed": 2,
        "recovered_by_client_retry": 1,
        "failed": 1,
    }


def test_percentile_interpolates():
    assert percentile([10, 20, 30, 40], 50) == 25
    assert percentile([], 95) is None


def test_detection_signals_from_logs_and_events():
    logs = [
        {"t": 3.0, "container": "gateway", "text": "Server payment/payment-1 is DOWN, reason: x"},
        {"t": 2.0, "container": "student-1", "text": "{}", "json": {"event": "breaker_opened"}},
        {"t": 1.0, "container": "student-1", "text": "{}", "json": {"event": "other"}},
    ]
    events = [
        {
            "Action": "health_status: unhealthy",
            "timeNano": 4e9,
            "Actor": {"Attributes": {"name": "p"}},
        }
    ]

    assert detection_signals(logs, events) == [
        (2.0, "student-1:breaker_opened"),
        (3.0, "gateway:haproxy_server_down"),
        (4.0, "p:unhealthy"),
    ]


def test_starts_requested_by_repair_are_not_automatic():
    events = [
        {"Action": "start", "timeNano": 50e9, "Actor": {"Attributes": {"name": "payment"}}},
        {"Action": "start", "timeNano": 30e9, "Actor": {"Attributes": {"name": "records"}}},
        {"Action": "start", "timeNano": 0.5e9, "Actor": {"Attributes": {"name": "boot"}}},
    ]
    actions = [{"do": "repair", "t": 49.5, "result": {"started": ["payment"]}}]

    assert automatic_restarts(events, actions, t0=0) == 1


def test_load_steps_split_the_run_by_rate():
    attempts = load(4, failing={3})

    steps = load_steps(attempts, t0=0, profile=[(2, 10), (2, 10)])

    assert [s["attempts"] for s in steps] == [20, 20]
    assert [s["availability"] for s in steps] == [1.0, 0.5]


def test_answer_slower_than_the_slo_is_not_good():
    stats = request_stats([attempt(0, 200, ms=2500), attempt(1, 409, ms=100), attempt(2, 0)])

    assert stats["ok"] == 1
    assert stats["failures"] == {"slow": 1, "0": 1}


def test_injected_failures_last_until_the_next_repair():
    actions = [
        {"t": 10, "do": "crash"},
        {"t": 25, "do": "repair"},
        {"t": 40, "do": "toxic"},
        {"t": 45, "do": "repair"},
        {"t": 90, "do": "kill"},
    ]

    assert injected(actions, t_end=100) == {
        "faults": 3,
        "fault_time_s": 30.0,
        "mean_fault_time_s": 10.0,
    }
