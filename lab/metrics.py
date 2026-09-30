"""Metrics of one run, computed from the request log, service logs and Docker events.

A request is *good* when the system answered it without a server error within 2 s (an SLO in
the sense of the SRE book; a user waiting longer is not served). The 2 s limit also bounds the
error of assigning a request to the second it was sent in.

Notation follows the lecture (Week 1). A second of the run is *unavailable* when less than
95% of the requests sent in it were good. A run of unavailable seconds is one outage
(failure event, D_i is its length); outages closer than MERGE_GAP_S are merged. Then
    D_total = sum(D_i), U_total = T_obs - D_total, A = U_total / (U_total + D_total),
    MTTF = U_total / N, MTTR = D_total / N, MTBF = T_obs / N = MTTF + MTTR.
"""

import contextlib
import json
import math
from collections import Counter
from dataclasses import dataclass

from lab.loadgen import Attempt, is_success

THRESHOLD = 0.95
SLO_LATENCY_MS = 2000
MERGE_GAP_S = 2
BUCKET_S = 1.0

# Log events that show the system itself noticed a failure (emitted by the FT mechanisms).
DETECTION_EVENTS = {
    "breaker_opened",
    "dependency_unavailable",
    "db_unavailable",
    "load_shed",
    "primary_lost",
    "replica_unhealthy",
}
# HAProxy: "is DOWN" comes from a health check, "is going DOWN" from DNS resolution.
DETECTION_TEXT = {" is DOWN": "haproxy_server_down", " is going DOWN": "haproxy_server_down"}
# Log events of work that failed first and was then completed by a mechanism.
RECOVERY_EVENTS = {
    "retry_succeeded",
    "payment_reconciled",
    "registration_verified",
    "job_resumed",
    "alternate_used",
    "copy_repaired",
    "watchdog_restart",
}
# Everything the fault-tolerance mechanisms log, counted per run to show which of them
# worked. Some are rate limited in the services (at most once per 5 s per kind).
MECHANISM_EVENTS = (
    DETECTION_EVENTS
    | RECOVERY_EVENTS
    | {
        "breaker_half_open",
        "breaker_closed",
        "retry_attempt",
        "duplicate_request",
        "payment_deferred",
        "fallback_used",
        "registration_rejected",
        "job_paused",
        "job_taken_over",
        "version_outvoted",
    }
)


def good(a: Attempt) -> bool:
    return is_success(a.status) and a.latency_ms <= SLO_LATENCY_MS


def failure_kind(a: Attempt) -> str:
    if a.error:
        return a.error
    return "slow" if is_success(a.status) else str(a.status)


@dataclass(frozen=True)
class Bucket:
    start: float
    total: int
    ok: int

    @property
    def ratio(self) -> float:
        return self.ok / self.total if self.total else 1.0


def buckets(attempts: list[Attempt], t0: float, t1: float, service: str | None = None) -> list:
    count = max(math.ceil((t1 - t0) / BUCKET_S), 0)
    total, ok = [0] * count, [0] * count
    for a in attempts:
        index = int((a.t - t0) // BUCKET_S)
        if 0 <= index < count and (service is None or a.service == service):
            total[index] += 1
            ok[index] += good(a)
    return [Bucket(t0 + i * BUCKET_S, total[i], ok[i]) for i in range(count)]


def unavailable(bucket_list: list[Bucket]) -> list[bool]:
    """Empty seconds (no requests of this kind) keep the state of the previous second."""
    flags, previous = [], False
    for bucket in bucket_list:
        previous = bucket.ratio < THRESHOLD if bucket.total else previous
        flags.append(previous)
    return flags


def outages(bucket_list: list[Bucket]) -> list[tuple[float, float]]:
    spans: list[list[float]] = []
    for bucket, down in zip(bucket_list, unavailable(bucket_list), strict=True):
        if not down:
            continue
        end = bucket.start + BUCKET_S
        if spans and bucket.start - spans[-1][1] <= MERGE_GAP_S * BUCKET_S:
            spans[-1][1] = end
        else:
            spans.append([bucket.start, end])
    return [(start, end) for start, end in spans]


def reliability(spans: list[tuple[float, float]], t_obs: float) -> dict:
    n = len(spans)
    d_total = sum(end - start for start, end in spans)
    u_total = t_obs - d_total
    return {
        "T_obs_s": round(t_obs, 1),
        "N_failures": n,
        "D_total_s": round(d_total, 1),
        "U_total_s": round(u_total, 1),
        "availability": round(u_total / t_obs, 5) if t_obs else None,
        "MTTF_s": round(u_total / n, 1) if n else None,
        "MTTR_s": round(d_total / n, 1) if n else None,
        "MTBF_s": round(t_obs / n, 1) if n else None,
        "failure_rate_per_h": round(n / u_total * 3600, 2) if n and u_total else 0.0,
    }


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = q / 100 * (len(ordered) - 1)
    low, high = math.floor(rank), math.ceil(rank)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 1)


def request_stats(attempts: list[Attempt]) -> dict:
    ok = sum(good(a) for a in attempts)
    answered = [a.latency_ms for a in attempts if a.status]
    return {
        "attempts": len(attempts),
        "ok": ok,
        "failed": len(attempts) - ok,
        "availability": round(ok / len(attempts), 5) if attempts else None,
        "failures": dict(Counter(failure_kind(a) for a in attempts if not good(a)).most_common()),
        "latency_ms": {q: percentile(answered, int(q[1:])) for q in ("p50", "p95", "p99")},
    }


def payment_intents(attempts: list[Attempt]) -> dict:
    """Client-side view of payments: an intent is one payment with all its retries. Uses the
    client's own rule (any answer that is not a server error), not the latency SLO."""
    by_intent: dict[str, list[Attempt]] = {}
    for a in attempts:
        if a.intent:
            by_intent.setdefault(a.intent, []).append(a)
    first_failed = recovered = given_up = 0
    for tries in by_intent.values():
        tries.sort(key=lambda a: a.attempt)
        if is_success(tries[0].status):
            continue
        first_failed += 1
        if any(is_success(a.status) for a in tries[1:]):
            recovered += 1
        else:
            given_up += 1
    return {
        "intents": len(by_intent),
        "first_attempt_failed": first_failed,
        "recovered_by_client_retry": recovered,
        "failed": given_up,
    }


def parse_logs(lines: list[dict]) -> list[dict]:
    """lines: {"t", "container", "text"}; JSON log lines get their fields parsed."""
    parsed = []
    for line in lines:
        entry = {"t": line["t"], "container": line["container"], "text": line["text"]}
        if line["text"].startswith("{"):
            with contextlib.suppress(json.JSONDecodeError):
                entry["json"] = json.loads(line["text"])
        parsed.append(entry)
    return parsed


def detection_signals(logs: list[dict], events: list[dict]) -> list[tuple[float, str]]:
    signals = []
    for entry in logs:
        event = entry.get("json", {}).get("event")
        if event in DETECTION_EVENTS:
            signals.append((entry["t"], f"{entry['container']}:{event}"))
            continue
        for needle, name in DETECTION_TEXT.items():
            if needle in entry["text"]:
                signals.append((entry["t"], f"{entry['container']}:{name}"))
                break
    for event in events:
        if event.get("Action") == "health_status: unhealthy":
            name = event.get("Actor", {}).get("Attributes", {}).get("name", "")
            signals.append((event["timeNano"] / 1e9, f"{name}:unhealthy"))
    return sorted(signals)


def recovered_by_system(logs: list[dict]) -> int:
    return sum(entry.get("json", {}).get("event") in RECOVERY_EVENTS for entry in logs)


def mechanism_events(logs: list[dict]) -> dict[str, int]:
    events = (entry.get("json", {}).get("event") for entry in logs)
    return dict(sorted(Counter(e for e in events if e in MECHANISM_EVENTS).items()))


def automatic_restarts(events: list[dict], actions: list[dict], t0: float) -> int:
    """Container starts that no repair action of the lab asked for."""
    manual = [
        (record["t"], name)
        for record in actions
        if record.get("do") == "repair"
        for key in ("started", "restarted_hung")
        for name in record.get("result", {}).get(key, [])
    ]
    count = 0
    for event in events:
        if event.get("Action") != "start" or event["timeNano"] / 1e9 < t0 + 1:
            continue
        name = event.get("Actor", {}).get("Attributes", {}).get("name", "")
        t = event["timeNano"] / 1e9
        if not any(n == name and 0 <= t - t_repair < 15 for t_repair, n in manual):
            count += 1
    return count


def fault_metrics(
    attempts: list[Attempt], fault_at: float, t_end: float, signals: list[tuple[float, str]]
) -> dict:
    after = [a for a in attempts if a.t >= fault_at]
    failures = [a.t for a in after if not good(a)]
    spans = outages(buckets(attempts, fault_at, t_end))
    detected = next(((t, source) for t, source in signals if t >= fault_at), None)
    return {
        "client_impact_s": round(min(failures) - fault_at, 2) if failures else None,
        "detection_s": round(detected[0] - fault_at, 2) if detected else None,
        "detection_source": detected[1] if detected else None,
        "recovery_s": round(spans[-1][1] - fault_at, 1) if spans else 0.0,
        # False when the last outage lasted until the end of the run.
        "recovered": not spans or spans[-1][1] < t_end - BUCKET_S / 2,
        "downtime_s": round(sum(end - start for start, end in spans), 1),
        "outages": len(spans),
        "requests": request_stats(after),
    }


def load_steps(attempts: list[Attempt], t0: float, profile: list[tuple[float, float]]) -> list:
    """Request statistics for every step of the rate profile (used for calibration)."""
    steps, start = [], t0
    for duration, rate in profile:
        inside = [a for a in attempts if start <= a.t < start + duration]
        stats = request_stats(inside)
        steps.append(
            {
                "rate": rate,
                "attempts": stats["attempts"],
                "availability": stats["availability"],
                "latency_ms": stats["latency_ms"],
                "failures": stats["failures"],
            }
        )
        start += duration
    return steps


def compute(
    attempts: list[Attempt],
    t0: float,
    t_end: float,
    fault_at: float | None,
    logs: list[dict],
    events: list[dict],
    actions: list[dict],
    profile: list[tuple[float, float]],
) -> dict:
    services = sorted({a.service for a in attempts})
    all_spans = outages(buckets(attempts, t0, t_end))
    signals = detection_signals(logs, events)
    return {
        "requests": request_stats(attempts),
        "reliability": reliability(all_spans, t_end - t0),
        "fault": fault_metrics(attempts, fault_at, t_end, signals) if fault_at else None,
        "load_steps": load_steps(attempts, t0, profile) if len(profile) > 1 else None,
        "per_service": {
            name: {
                "availability": request_stats([a for a in attempts if a.service == name])[
                    "availability"
                ],
                "downtime_s": round(
                    sum(e - s for s, e in outages(buckets(attempts, t0, t_end, name))), 1
                ),
            }
            for name in services
        },
        "payments": payment_intents(attempts),
        "recovered_by_system": recovered_by_system(logs),
        "mechanisms": mechanism_events(logs),
        "automatic_restarts": automatic_restarts(events, actions, t0),
        "detection_signals": [
            {"t_s": round(t - t0, 2), "source": source} for t, source in signals[:20]
        ],
    }
