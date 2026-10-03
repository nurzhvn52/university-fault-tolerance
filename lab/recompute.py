"""Recomputes metrics.json of stored runs from their raw files.

    python -m lab.recompute --tag final

Metrics are derived data: when a definition in lab/metrics.py changes, every run can be
re-evaluated without repeating the experiment. Runs on the host (dev requirements).
"""

import argparse
import gzip
import json
from pathlib import Path

from lab import consistency, metrics
from lab.loadgen import read_attempts
from lab.scenarios import load

ROOT = Path(__file__).resolve().parent.parent


def read_jsonl_gz(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def recompute(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    scenario = load(seed=meta["seed"])[meta["scenario"]]
    t0 = meta["t0"]
    result = metrics.compute(
        read_attempts(run_dir / "requests.csv.gz"),
        t0,
        t0 + scenario.duration,
        t0 + scenario.fault_at if scenario.fault_at is not None else None,
        metrics.parse_logs(read_jsonl_gz(run_dir / "logs.jsonl.gz")),
        read_jsonl_gz(run_dir / "events.jsonl.gz"),
        json.loads((run_dir / "actions.json").read_text(encoding="utf-8")),
        scenario.rate_profile,
    )
    report = json.loads((run_dir / "consistency.json").read_text(encoding="utf-8"))
    result["consistency"] = consistency.violations(report)
    (run_dir / "metrics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="dev")
    args = parser.parse_args()
    runs = sorted(p.parent for p in (ROOT / "results" / args.tag).glob("*/*/rep*/meta.json"))
    for run_dir in runs:
        recompute(run_dir)
    print(f"recomputed {len(runs)} runs")


if __name__ == "__main__":
    main()
