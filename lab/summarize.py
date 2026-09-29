"""Aggregates the runs of a tag into one table (mean, std, min, max over repetitions).

    python -m lab.summarize --tag final

Writes results/<tag>/summary.csv and results/<tag>/summary.json.
"""

import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

METRICS = {
    "availability": ("requests", "availability"),
    "failed_requests": ("requests", "failed"),
    "p95_ms": ("requests", "latency_ms", "p95"),
    "client_impact_s": ("fault", "client_impact_s"),
    "detection_s": ("fault", "detection_s"),
    "recovery_s": ("fault", "recovery_s"),
    "downtime_s": ("fault", "downtime_s"),
    "fault_window_failed": ("fault", "requests", "failed"),
    "MTTR_s": ("reliability", "MTTR_s"),
    "payments_recovered_by_retry": ("payments", "recovered_by_client_retry"),
    "recovered_by_system": ("recovered_by_system",),
    "automatic_restarts": ("automatic_restarts",),
}


def dig(data, path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def describe(values: list) -> dict:
    numbers = [v for v in values if isinstance(v, int | float)]
    if not numbers:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None}
    return {
        "n": len(numbers),
        "mean": round(statistics.fmean(numbers), 4),
        "std": round(statistics.stdev(numbers), 4) if len(numbers) > 1 else 0.0,
        "min": min(numbers),
        "max": max(numbers),
    }


def summarize(tag_dir: Path) -> list[dict]:
    rows = []
    for scenario_dir in sorted(p for p in tag_dir.glob("*/*") if p.is_dir()):
        runs = [
            json.loads(f.read_text(encoding="utf-8"))
            for f in sorted(scenario_dir.glob("rep*/metrics.json"))
        ]
        if not runs:
            continue
        row = {"mode": scenario_dir.parent.name, "scenario": scenario_dir.name, "runs": len(runs)}
        for name, path in METRICS.items():
            row[name] = describe([dig(run, path) for run in runs])
        checks = sorted({check for run in runs for check in run.get("consistency", {})})
        row["consistency"] = {
            check: describe([run["consistency"].get(check) for run in runs]) for check in checks
        }
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="dev")
    args = parser.parse_args()
    tag_dir = ROOT / "results" / args.tag
    rows = summarize(tag_dir)
    (tag_dir / "summary.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    with open(tag_dir / "summary.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["mode", "scenario", "runs", "metric", "mean", "std", "min", "max"])
        for row in rows:
            stats = {**{k: row[k] for k in METRICS}, **row["consistency"]}
            for metric, value in stats.items():
                writer.writerow(
                    [row["mode"], row["scenario"], row["runs"], metric]
                    + [value[k] for k in ("mean", "std", "min", "max")]
                )
    print(f"{len(rows)} scenario/mode groups -> {tag_dir / 'summary.csv'}")


if __name__ == "__main__":
    main()
