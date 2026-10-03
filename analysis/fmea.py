"""FMEA of the portal (Week 2: failure mode, cause, effect, severity, occurrence, detection,
mitigation). Scores are engineering judgement on a 1-10 scale (10 = worst / most frequent /
hardest to detect), before (baseline) and after (fault-tolerant version); RPN = S x O x D.
The experiment that exercised each mode is given where there is one.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FailureMode:
    component: str
    mode: str
    cause: str
    effect: str
    baseline: tuple[int, int, int]  # severity, occurrence, detection
    mitigation: str
    ft: tuple[int, int, int]
    experiment: str = ""

    @staticmethod
    def rpn(scores: tuple[int, int, int]) -> int:
        severity, occurrence, detection = scores
        return severity * occurrence * detection


ROWS = [
    FailureMode("service instance", "crash", "software bug, out of memory",
                "requests to the service fail until someone restarts it", (7, 5, 7),
                "second replica, gateway health checks, restart policy", (3, 5, 2), "E1"),
    FailureMode("service instance", "hang", "blocked event loop, deadlock",
                "requests time out and hold resources", (8, 3, 8),
                "timeouts, health checks, watchdog restart", (3, 3, 3), "E1b"),
    FailureMode("database", "primary down", "server crash, disk full",
                "writes and most reads fail", (10, 3, 6),
                "synchronous standby, Patroni failover, cached reads", (5, 3, 2), "E2"),
    FailureMode("network", "timeout between services", "congestion, lost packets",
                "cascading timeouts, exhausted connection pool", (7, 5, 7),
                "timeouts, circuit breaker, graceful degradation", (3, 5, 2), "E3"),
    FailureMode("node", "server failure", "power supply, kernel panic",
                "every component on the node is down", (10, 2, 5),
                "second node, etcd quorum on three nodes", (5, 2, 2), "E4"),
    FailureMode("payment", "interrupted transaction", "crash between bank charge and record",
                "money taken without a record", (9, 4, 9),
                "journal (PENDING first), idempotent charge, reconciliation", (3, 4, 2), "E5a"),
    FailureMode("payment", "duplicate request", "client retry after a timeout",
                "student charged twice", (9, 6, 9),
                "idempotency keys, duplicate detection", (2, 6, 1), "E2, E5a"),
    FailureMode("registration", "race on the seat counter", "concurrent read-modify-write",
                "overbooked sections, wrong counters", (7, 6, 8),
                "atomic conditional update", (2, 6, 1), "E6"),
    FailureMode("grade import", "partial batch", "crash in the middle of an import",
                "half-imported grades, repeat fails", (6, 3, 8),
                "one transaction, idempotent upsert", (2, 3, 2), "E5b"),
    FailureMode("timetable job", "lost progress", "crash during generation",
                "job stuck in RUNNING, timetable not published", (5, 3, 7),
                "checkpoints, leases, resume; recovery block", (2, 3, 2), "E5c"),
    FailureMode("storage", "corrupted or lost file", "bit rot, failed disk",
                "transcript documents lost or wrong", (8, 2, 9),
                "three mirrored copies, majority vote, scrub", (3, 2, 2), "E9"),
    FailureMode("memory", "bit flip", "radiation, worn cells",
                "silent data corruption", (8, 2, 10),
                "ECC memory (SEC-DED), documented and simulated", (2, 2, 2), "ECC simulation"),
    FailureMode("load", "overload", "registration opening rush",
                "timeouts for everyone, collapse", (8, 7, 6),
                "replicas, load shedding, bounded queues", (4, 7, 2), "E6"),
    FailureMode("gateway", "crash", "software bug, host failure",
                "the whole portal is unavailable", (10, 2, 5),
                "monitored, restart policy; still a single point of failure", (10, 2, 2), ""),
]  # fmt: skip


def table() -> list[dict]:
    rows = []
    for row in ROWS:
        data = asdict(row)
        data["rpn_baseline"] = FailureMode.rpn(row.baseline)
        data["rpn_ft"] = FailureMode.rpn(row.ft)
        rows.append(data)
    return sorted(rows, key=lambda r: -r["rpn_baseline"])
