"""What the monitoring stack saw during a run: alerts, notifications and a few time series.

Prometheus keeps ``ALERTS{alertstate="firing"}`` as a time series, so the moment an alert
started and stopped firing can be read back after the run; the alert sink records when each
Alertmanager notification arrived. Detection time is measured from the injection of a fault
to the first firing alert that the scenario expects, notification time to the arrival of its
notification.
"""

import asyncio
import time
from dataclasses import dataclass, field

import httpx

STEP_S = 1
# Series stored with every run, enough to redraw the dashboard afterwards.
SERIES = {
    "error_ratio": (
        '(sum(rate(uft_requests_total{status=~"5.."}[10s])) or vector(0))'
        " / sum(rate(uft_requests_total[10s]))"
    ),
    "requests_per_s": "sum(rate(uft_requests_total[10s]))",
    "p95_s": "histogram_quantile(0.95, sum by (le) (rate(uft_request_seconds_bucket[15s])))",
    "targets_up": 'sum(up{job="services"})',
    "healthy_replicas": (
        'sum(haproxy_backend_active_servers{proxy=~"auth|student|payment|records|timetable"})'
    ),
    "primary": "sum(patroni_primary)",
    "denied_per_s": "sum(rate(uft_auth_denied_total[10s])) or vector(0)",
    "rate_limited_per_s": "sum(rate(uft_rate_limited_total[10s])) or vector(0)",
    "shed_per_s": "sum(rate(uft_load_shed_total[10s])) or vector(0)",
}


async def firing_now(http: httpx.AsyncClient, prometheus: str) -> list[str]:
    response = await http.get(f"{prometheus}/api/v1/alerts", timeout=5)
    response.raise_for_status()
    alerts = response.json()["data"]["alerts"]
    return sorted({a["labels"]["alertname"] for a in alerts if a["state"] == "firing"})


async def wait_quiet(http: httpx.AsyncClient, prometheus: str, timeout_s: float = 120) -> list[str]:
    """Waits until no alert fires (start-up of the stack raises some). Returns what still
    fired when the wait ended."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            names = await firing_now(http, prometheus)
        except httpx.HTTPError:
            names = ["prometheus_unreachable"]
        if not names or time.monotonic() > deadline:
            return names
        await asyncio.sleep(2)


async def _range(http: httpx.AsyncClient, prometheus: str, query: str, start: float, end: float):
    response = await http.get(
        f"{prometheus}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": STEP_S},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"]["result"]


async def collect(
    http: httpx.AsyncClient, prometheus: str, sink: str, t0: float, t_end: float
) -> dict:
    """Firing intervals of every alert, notifications and the stored time series."""
    start, end = t0 - 5, t_end + 5
    firing = []
    for series in await _range(http, prometheus, 'ALERTS{alertstate="firing"}', start, end):
        times = [float(t) for t, _ in series["values"]]
        labels = {k: v for k, v in series["metric"].items() if k not in ("__name__", "alertstate")}
        # A series can fire, stop and fire again: split at gaps.
        intervals, begin, last = [], times[0], times[0]
        for t in times[1:]:
            if t - last > 3 * STEP_S:
                intervals.append((begin, last))
                begin = t
            last = t
        intervals.append((begin, last))
        firing.extend(
            {"alert": labels["alertname"], "labels": labels, "from": a, "to": b}
            for a, b in intervals
        )
    received = (await http.get(f"{sink}/received", timeout=10)).json()
    notifications = [
        {
            "alert": n["alertname"],
            "status": n["status"],
            "severity": n["severity"],
            "received_at": n["received_at"],
            "labels": n["labels"],
        }
        for n in received
        if start <= n["received_at"] <= end
    ]
    series = {}
    for name, query in SERIES.items():
        result = await _range(http, prometheus, query, start, end)
        series[name] = [[float(t), float(v)] for t, v in result[0]["values"]] if result else []
    return {
        "firing": sorted(firing, key=lambda a: a["from"]),
        "notifications": notifications,
        "series": series,
    }


@dataclass
class Window:
    """A stretch of a scenario with the alerts it is meant to raise."""

    start: float
    end: float
    expect: list[str] = field(default_factory=list)
    tolerate: list[str] = field(default_factory=list)


def evaluate(collected: dict, t0: float, windows: list[Window], grace_s: float = 15.0) -> dict:
    """Each expected alert must start firing inside its window (or within ``grace_s`` after
    it); every other alert that starts firing during the run, and is not tolerated in the
    window it starts in, is a false positive."""
    firing = collected["firing"]
    notifications = collected["notifications"]
    results, claimed = [], set()
    for window in windows:
        lo, hi = t0 + window.start, t0 + window.end + grace_s
        for name in window.expect:
            # An alert can fire for several label sets at once (one per service); all of them
            # belong to the expected alert.
            hits = [
                i
                for i, a in enumerate(firing)
                if a["alert"] == name and lo <= a["from"] <= hi and i not in claimed
            ]
            row = {"alert": name, "window_s": [window.start, window.end], "fired": bool(hits)}
            if hits:
                claimed.update(hits)
                first = min(firing[i]["from"] for i in hits)
                row["detection_s"] = round(first - lo, 1)
                note = min(
                    (
                        n["received_at"]
                        for n in notifications
                        if n["alert"] == name
                        and n["status"] == "firing"
                        and n["received_at"] >= first - 1
                    ),
                    default=None,
                )
                row["notification_s"] = round(note - lo, 1) if note is not None else None
            results.append(row)
    false_positives = []
    for i, alert in enumerate(firing):
        if i in claimed or alert["from"] < t0 - 1:
            continue
        offset = alert["from"] - t0
        tolerated = any(
            w.start <= offset <= w.end + grace_s and alert["alert"] in w.tolerate for w in windows
        )
        if not tolerated:
            false_positives.append(
                {"alert": alert["alert"], "at_s": round(offset, 1), "labels": alert["labels"]}
            )
    expected = len(results)
    detected = sum(r["fired"] for r in results)
    return {
        "expected": results,
        "expected_total": expected,
        "detected": detected,
        "missed": expected - detected,
        "false_positives": false_positives,
        "false_positive_count": len(false_positives),
        "fired": sorted({a["alert"] for a in firing if a["from"] >= t0 - 1}),
    }
