"""Evaluation of the endterm experiments (scenarios X1-X8, modes ft and ft2).

    python -m analysis.run_endterm --tag final

Everything in the report tables comes from here; the numbers are computed from the stored
runs, nothing is typed in by hand.
"""

import json
from collections import Counter

from analysis.results import stats
from lab.loadgen import read_attempts

MODES = ("ft", "ft2")
SCENARIOS = ("X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8")
# Findings about the audit trail itself, kept apart from damage to business data.
AUDIT_CHECKS = {"lost_from_table", "denial_reasons_without_event"}
# Statements a taken-over service must not be able to run.
FORBIDDEN_STATEMENTS = (
    "other_schema_read",
    "credentials_read",
    "audit_read",
    "audit_tamper_update",
    "audit_tamper_delete",
    "ddl_create",
    "ddl_drop",
)


def _business(consistency: dict) -> int:
    return sum(v for k, v in consistency.items() if k not in AUDIT_CHECKS)


def _audit(consistency: dict) -> int:
    return sum(v for k, v in consistency.items() if k in AUDIT_CHECKS)


def _alert_rows(metrics: list[dict]) -> dict:
    expected = [e for m in metrics for e in (m.get("alerts") or {}).get("expected", [])]
    by_alert: dict[str, dict] = {}
    for row in expected:
        entry = by_alert.setdefault(
            row["alert"], {"expected": 0, "fired": 0, "detection_s": [], "notification_s": []}
        )
        entry["expected"] += 1
        entry["fired"] += row["fired"]
        if row["fired"]:
            entry["detection_s"].append(row["detection_s"])
            if row.get("notification_s") is not None:
                entry["notification_s"].append(row["notification_s"])
    false_positives = Counter(
        fp["alert"] for m in metrics for fp in (m.get("alerts") or {}).get("false_positives", [])
    )
    return {
        "by_alert": {
            name: {
                "expected": e["expected"],
                "fired": e["fired"],
                "detection_s": stats(e["detection_s"]),
                "notification_s": stats(e["notification_s"]),
            }
            for name, e in sorted(by_alert.items())
        },
        "expected_total": len(expected),
        "detected_total": sum(r["fired"] for r in expected),
        "false_positives": dict(false_positives),
        "false_positive_total": sum(false_positives.values()),
    }


def _mean_latency_ms(run: dict) -> float | None:
    """Mean over answered requests (the metrics file keeps percentiles only)."""
    latencies = [a.latency_ms for a in read_attempts(run["dir"] / "requests.csv.gz") if a.status]
    return sum(latencies) / len(latencies) if latencies else None


def _security(scenario: str, metrics: list[dict]) -> dict:
    sec = [m.get("security") or {} for m in metrics]
    if scenario in ("X7", "X8"):
        key = "probe_unauthorized" if scenario == "X7" else "probe_malformed"
        probes = [s[key] for s in sec if key in s]
        out = {
            "probes": sum(p["total"] for p in probes),
            "refused": sum(p["refused"] for p in probes),
            "succeeded": sum(p["succeeded"] for p in probes),
            "leaked": sum(p["leaked"] for p in probes),
            "server_errors": sum(p["server_errors"] for p in probes),
            "runs_with_probes": len(probes),
        }
        data_changed = [
            sum(v for k, v in m["consistency"].items() if k.startswith(("victim_", "poisoned")))
            for m in metrics
        ]
        out["victim_data_changes"] = sum(data_changed)
        floods = [f for s in sec for f in s.get("floods", [])]
        if floods:
            sent = sum(f["sent"] for f in floods)
            throttled = sum(f["statuses"].get("429", 0) for f in floods)
            out["flood"] = {
                "sent": sent,
                "served": sum(f["statuses"].get("200", 0) for f in floods),
                "throttled": throttled,
                "throttled_share": round(throttled / sent, 3) if sent else None,
                "attacker_p95_ms": stats([f["latency_ms"]["p95"] for f in floods]),
            }
        return out
    if scenario == "X9":
        attacks = [a for s in sec for a in s.get("login_attacks", [])]
        logins = [lg["status"] for s in sec for lg in s.get("legit_logins", [])]
        one = [a for a in attacks if a["accounts"] == 1]
        spray = [a for a in attacks if a["accounts"] > 1]
        return {
            "single_account_first_block": stats([a["first_block_at_attempt"] for a in one]),
            "single_account_statuses": _sum_counters([a["statuses"] for a in one]),
            "spray_first_block": stats([a["first_block_at_attempt"] for a in spray]),
            "spray_statuses": _sum_counters([a["statuses"] for a in spray]),
            "legit_logins": dict(Counter(map(str, logins))),
        }
    if scenario == "X10":
        probes = [p for s in sec for p in s.get("privilege", [])]
        roles: dict[str, dict] = {}
        for p in probes:
            row = roles.setdefault(
                p["role"], {"runs": 0, "allowed": Counter(), "denied": Counter()}
            )
            row["runs"] += 1
            for name, result in p.items():
                if name == "role":
                    continue
                (row["allowed"] if result == "allowed" else row["denied"])[name] += 1
        return {
            role: {
                "runs": row["runs"],
                "forbidden_allowed": sum(row["allowed"][n] for n in FORBIDDEN_STATEMENTS),
                "forbidden_total": row["runs"] * len(FORBIDDEN_STATEMENTS),
                "allowed_statements": sorted(row["allowed"]),
            }
            for role, row in sorted(roles.items())
        }
    return {}


def _sum_counters(counters: list[dict]) -> dict:
    total: Counter = Counter()
    for c in counters:
        total.update(c)
    return dict(total)


def summarize(runs: dict) -> dict:
    """{scenario: {mode: {...}}} over the repetitions of every combination."""
    table: dict = {}
    for (mode, scenario), group in sorted(runs.items()):
        if scenario not in SCENARIOS:
            continue
        metrics = [run["metrics"] for run in group]
        fault = [m["fault"] or {} for m in metrics]
        during = [f.get("requests") or {} for f in fault]
        table.setdefault(scenario, {})[mode] = {
            "runs": len(group),
            "availability": stats([m["requests"]["availability"] for m in metrics]),
            "availability_after_fault": stats([d.get("availability") for d in during]),
            "failed": stats([m["requests"]["failed"] for m in metrics]),
            "attempts": stats([m["requests"]["attempts"] for m in metrics]),
            "mean_ms": stats([_mean_latency_ms(run) for run in group]),
            "p50_ms": stats([m["requests"]["latency_ms"]["p50"] for m in metrics]),
            "p95_ms": stats([m["requests"]["latency_ms"]["p95"] for m in metrics]),
            "p95_after_fault_ms": stats([(d.get("latency_ms") or {}).get("p95") for d in during]),
            "recovery_s": stats([f.get("recovery_s") for f in fault]),
            "detection_s": stats([f.get("detection_s") for f in fault]),
            "availability_time": stats([m["reliability"]["availability"] for m in metrics]),
            "business_violations": stats([_business(m["consistency"]) for m in metrics]),
            "audit_findings": stats([_audit(m["consistency"]) for m in metrics]),
            "violations_by_check": {
                check: stats([m["consistency"].get(check, 0) for m in metrics])["mean"]
                for check in sorted({c for m in metrics for c in m["consistency"]})
                if any(m["consistency"].get(check, 0) for m in metrics)
            },
            "alerts": _alert_rows(metrics) if mode == "ft2" else None,
            "security": _security(scenario, metrics),
        }
    return table


def alert_catalog(rules_path) -> list[dict]:
    """Alert rules with their documented threshold and response (infra/monitoring/rules.yml)."""
    import yaml

    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))["groups"][0]["rules"]
    return [
        {
            "alert": r["alert"],
            "severity": r["labels"]["severity"],
            "detects": r["annotations"]["detects"],
            "threshold": r["annotations"]["threshold"],
            "response": r["annotations"]["response"],
        }
        for r in rules
    ]


def dump(path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
