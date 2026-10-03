"""Evaluation of the experiment results: comparison tables and the long-run prediction.

The long-run prediction checks whether the single-failure experiments explain the long run
E7: every failure E7 injected is replaced by the outage its single-failure scenario showed
in the same mode (E1 crash, E1b hang, E2 database, E3 network, E4 node), and the predicted
availability is compared with the measured one.
"""

import json
import statistics
from pathlib import Path

from lab.loadgen import read_attempts
from lab.metrics import buckets, outages

ROOT = Path(__file__).resolve().parent.parent
MODES = ("baseline", "sw", "ft")
SCENARIOS = ("E1", "E1b", "E2", "E3", "E4", "E5a", "E5b", "E5c", "E6", "E9", "E7")
# Failure type in E7 -> the single-failure scenario that measured it.
TYPE_SCENARIO = {"crash": "E1", "hang": "E1b", "kill": "E2", "toxic": "E3", "kill_node": "E4"}
REPAIR_AFTER_S = 40  # E1-E4: failure at 20 s, repair at 60 s
RESTART_S = 3.0  # time to serve again after a repair when nothing else is known


def load_runs(tag: str) -> dict[tuple[str, str], list[dict]]:
    runs: dict[tuple[str, str], list[dict]] = {}
    for metrics_file in sorted((ROOT / "results" / tag).glob("*/*/rep*/metrics.json")):
        run_dir = metrics_file.parent
        run = {
            "dir": run_dir,
            "metrics": json.loads(metrics_file.read_text(encoding="utf-8")),
            "meta": json.loads((run_dir / "meta.json").read_text(encoding="utf-8")),
            "actions": json.loads((run_dir / "actions.json").read_text(encoding="utf-8")),
        }
        runs.setdefault((run_dir.parent.parent.name, run_dir.parent.name), []).append(run)
    return runs


def stats(values: list) -> dict:
    numbers = [v for v in values if isinstance(v, int | float) and v is not None]
    if not numbers:
        return {"n": 0, "mean": None, "std": None}
    return {
        "n": len(numbers),
        "mean": statistics.fmean(numbers),
        "std": statistics.stdev(numbers) if len(numbers) > 1 else 0.0,
    }


def comparison(runs: dict) -> dict:
    """Per scenario and mode: availability, failed requests, detection, recovery, downtime,
    share of runs that recovered and total consistency violations (mean over runs)."""
    table: dict = {}
    for (mode, scenario), group in runs.items():
        metrics = [run["metrics"] for run in group]
        fault = [m["fault"] or {} for m in metrics]
        table.setdefault(scenario, {})[mode] = {
            "runs": len(group),
            "availability": stats([m["requests"]["availability"] for m in metrics]),
            "failed": stats([m["requests"]["failed"] for m in metrics]),
            "p95_ms": stats([m["requests"]["latency_ms"]["p95"] for m in metrics]),
            "detection_s": stats([f.get("detection_s") for f in fault]),
            "recovery_s": stats([f.get("recovery_s") for f in fault]),
            "downtime_s": stats([f.get("downtime_s") for f in fault]),
            "recovered_share": stats([float(f.get("recovered", True)) for f in fault]),
            "violations": stats([sum(m["consistency"].values()) for m in metrics]),
            "violations_by_check": {
                check: stats([m["consistency"].get(check, 0) for m in metrics])["mean"]
                for check in sorted({c for m in metrics for c in m["consistency"]})
            },
            "mechanisms": {
                event: stats([m.get("mechanisms", {}).get(event, 0) for m in metrics])["mean"]
                for event in sorted({e for m in metrics for e in m.get("mechanisms", {})})
            },
        }
    return table


def outage_model(runs: dict, mode: str) -> dict[str, dict]:
    """Per failure type: the self-recovery time if the system recovered before the repair,
    otherwise the extra time it needed after the repair."""
    model = {}
    for kind, scenario in TYPE_SCENARIO.items():
        group = runs.get((mode, scenario), [])
        downtime = stats([(r["metrics"]["fault"] or {}).get("downtime_s") for r in group])["mean"]
        if downtime is None:
            continue
        if downtime < REPAIR_AFTER_S - 1:
            model[kind] = {"self_recovery_s": downtime, "after_repair_s": RESTART_S}
        else:
            model[kind] = {"self_recovery_s": None, "after_repair_s": downtime - REPAIR_AFTER_S}
    return model


def predict_outage(model: dict, kind: str, duration_s: float) -> float:
    entry = model[kind]
    after_repair = duration_s + entry["after_repair_s"]
    if entry["self_recovery_s"] is None:
        return after_repair
    return min(entry["self_recovery_s"], after_repair)


def charge_outages(
    faults: list[tuple[float, str]], spans: list[tuple[float, float]]
) -> list[float]:
    """Outage seconds caused by each fault. faults: (injected at, type) sorted by time; spans:
    (start, length) since the start of the load. An outage is charged to the last fault
    injected before it started, with 2 s tolerance for the one-second buckets."""
    per_fault = [0.0] * len(faults)
    for start, length in spans:
        index = max((i for i, (at, _) in enumerate(faults) if at <= start + 2), default=None)
        if index is not None:
            per_fault[index] += length
    return per_fault


def _outage_spans(run: dict) -> list[tuple[float, float]]:
    t0 = run["meta"]["t0"]
    t_obs = run["metrics"]["reliability"]["T_obs_s"]
    attempts = read_attempts(run["dir"] / "requests.csv.gz")
    return [(start - t0, end - start) for start, end in outages(buckets(attempts, t0, t0 + t_obs))]


def by_failure_type(group: list[dict]) -> dict:
    """E7: per failure type, how many failures became outages and how long those were."""
    found: dict[str, list[float]] = {kind: [] for kind in TYPE_SCENARIO}
    until_repair: dict[str, list[float]] = {kind: [] for kind in TYPE_SCENARIO}
    for run in group:
        actions = sorted(run["actions"], key=lambda a: a["at"])
        faults = [(a["at"], a["do"]) for a in actions if a["do"] in TYPE_SCENARIO]
        for (_, kind), down in zip(faults, charge_outages(faults, _outage_spans(run)), strict=True):
            found[kind].append(down)
        for i, action in enumerate(actions):
            if action["do"] in TYPE_SCENARIO:
                repair = next((a["at"] for a in actions[i + 1 :] if a["do"] == "repair"), None)
                if repair is not None:
                    until_repair[action["do"]].append(repair - action["at"])
    return {
        kind: {
            "faults": len(downs),
            "with_outage": sum(d > 0 for d in downs),
            "mean_outage_s": statistics.fmean([d for d in downs if d > 0] or [0.0]),
            "max_outage_s": max(downs),
            "mean_until_repair_s": statistics.fmean(until_repair[kind] or [0.0]),
        }
        for kind, downs in found.items()
        if downs
    }


def long_run(runs: dict) -> dict:
    """E7: measured reliability metrics and the availability predicted from E1-E4."""
    result = {}
    for mode in MODES:
        group = runs.get((mode, "E7"), [])
        if not group:
            continue
        model = outage_model(runs, mode)
        per_run = []
        for run in group:
            metrics, actions = run["metrics"], sorted(run["actions"], key=lambda a: a["t"])
            t_obs = metrics["reliability"]["T_obs_s"]
            predicted_down = 0.0
            predicted_failures = 0
            for i, action in enumerate(actions):
                if action["do"] not in TYPE_SCENARIO or action["do"] not in model:
                    continue
                repair = next(
                    (a["t"] for a in actions[i + 1 :] if a["do"] == "repair"), action["t"]
                )
                outage = predict_outage(model, action["do"], repair - action["t"])
                predicted_down += outage
                predicted_failures += outage >= 1
            per_run.append(
                {
                    "rep": run["meta"]["rep"],
                    "faults": metrics.get("injected", {}).get("faults"),
                    "fault_time_s": metrics.get("injected", {}).get("fault_time_s"),
                    "measured": metrics["reliability"],
                    "predicted_availability": 1 - predicted_down / t_obs,
                    "predicted_failures": predicted_failures,
                }
            )
        result[mode] = {
            "outage_model": model,
            "by_failure_type": by_failure_type(group),
            "runs": per_run,
            "availability": stats([r["measured"]["availability"] for r in per_run]),
            "predicted_availability": stats([r["predicted_availability"] for r in per_run]),
            "N_failures": stats([r["measured"]["N_failures"] for r in per_run]),
            "MTTF_s": stats([r["measured"]["MTTF_s"] for r in per_run]),
            "MTTR_s": stats([r["measured"]["MTTR_s"] for r in per_run]),
            "MTBF_s": stats([r["measured"]["MTBF_s"] for r in per_run]),
            "failure_rate_per_h": stats([r["measured"]["failure_rate_per_h"] for r in per_run]),
        }
    return result


def capacity(runs: dict) -> dict:
    """Load steps of the calibration runs: good share and p95 latency per offered rate."""
    result = {}
    for (mode, scenario), group in runs.items():
        if not scenario.startswith("CAL"):
            continue
        steps = group[0]["metrics"].get("load_steps") or []
        result.setdefault(scenario, {})[mode] = [
            {"rate": s["rate"], "availability": s["availability"], "p95_ms": s["latency_ms"]["p95"]}
            for s in steps
        ]
    return result
