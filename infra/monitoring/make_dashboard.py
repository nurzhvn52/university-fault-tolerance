"""Generates the Grafana dashboard of the endterm stack (grafana/dashboards/uft2.json).

python infra/monitoring/make_dashboard.py
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "grafana" / "dashboards" / "uft2.json"
DS = {"type": "prometheus", "uid": "prometheus"}

_panels: list[dict] = []


def panel(title, targets, *, kind="timeseries", unit=None, w=8, h=8, desc=""):
    index = len(_panels)
    per_row = 24 // w
    body = {
        "id": index + 1,
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": (index % per_row) * w, "y": (index // per_row) * h, "w": w, "h": h},
        "targets": [
            {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
            for i, (expr, legend) in enumerate(targets)
        ],
        "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []},
    }
    if kind == "table":
        body["targets"][0]["instant"] = True
        body["targets"][0]["format"] = "table"
    _panels.append(body)


panel(
    "Requests per second",
    [("sum by (service) (rate(uft_requests_total[10s]))", "{{service}}")],
    unit="reqps",
)
panel(
    "Error rate (5xx share)",
    [
        (
            'sum by (service) (rate(uft_requests_total{status=~"5.."}[10s]))'
            " / sum by (service) (rate(uft_requests_total[10s]))",
            "{{service}}",
        )
    ],
    unit="percentunit",
    desc="Alert HighErrorRate fires above 5%.",
)
panel(
    "p95 latency",
    [
        (
            "histogram_quantile(0.95, sum by (service, le)"
            " (rate(uft_request_seconds_bucket[15s])))",
            "{{service}}",
        )
    ],
    unit="s",
    desc="Alert HighLatencyP95 fires above 1 s.",
)
panel(
    "Alerts firing",
    [
        (
            "sum by (alertname, severity, service, instance, dependency)"
            ' (ALERTS{alertstate="firing"})',
            "",
        )
    ],
    kind="table",
)
panel(
    "Healthy replicas behind the gateway",
    [
        (
            'haproxy_backend_active_servers{proxy=~"student|payment|records|timetable"}',
            "{{proxy}}",
        )
    ],
)
panel("Database primary (Patroni)", [("patroni_primary", "{{instance}}")])
panel(
    "Dependency calls by outcome",
    [
        (
            "sum by (dependency, outcome) (rate(uft_dependency_calls_total[10s]))",
            "{{dependency}} {{outcome}}",
        )
    ],
)
panel(
    "Retries and open circuit breakers",
    [
        ("sum by (dependency) (rate(uft_retries_total[10s]))", "retries {{dependency}}"),
        ("max by (dependency) (uft_breaker_open)", "breaker open {{dependency}}"),
    ],
)
panel(
    "Load shed per second",
    [("sum by (service) (rate(uft_load_shed_total[10s]))", "{{service}}")],
)
panel(
    "Denied requests by reason",
    [("sum by (service, reason) (rate(uft_auth_denied_total[10s]))", "{{service}} {{reason}}")],
    desc="401/403 answers of the security layer.",
)
panel(
    "Rate limited requests",
    [("sum by (service, scope) (rate(uft_rate_limited_total[10s]))", "{{service}} {{scope}}")],
)
panel(
    "Audit events by outcome",
    [("sum by (action, outcome) (rate(uft_audit_events_total[10s]))", "{{action}} {{outcome}}")],
)
panel("Scrape targets up", [("up", "{{job}} {{instance}}")], w=24, h=7)

dashboard = {
    "uid": "uft2",
    "title": "University system - endterm: detection and security",
    "tags": ["uft", "endterm"],
    "timezone": "browser",
    "refresh": "2s",
    "time": {"from": "now-5m", "to": "now"},
    "schemaVersion": 39,
    "panels": _panels,
}
OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"wrote {OUT} ({len(_panels)} panels)")
