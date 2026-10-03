"""Builds the reliability analysis of a result set.

    python -m analysis.run --tag final

Writes results/<tag>/analysis.json (model, comparison tables, long-run evaluation, capacity,
FMEA) and the figures in docs/figures/.
"""

import argparse
import json
from pathlib import Path

from analysis import diagrams, figures, fmea, model, results

ROOT = Path(__file__).resolve().parent.parent
FIGURES = ROOT / "docs" / "figures"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="final")
    args = parser.parse_args()

    runs = results.load_runs(args.tag)
    comparison = results.comparison(runs)
    capacity = results.capacity(runs)
    analysis = {
        "model": model.summary(),
        "comparison": comparison,
        "long_run": results.long_run(runs),
        "capacity": capacity,
        "fmea": fmea.table(),
    }
    out = ROOT / "results" / args.tag / "analysis.json"
    out.write_text(json.dumps(analysis, indent=2, default=str) + "\n", encoding="utf-8")

    scenarios = [s for s in results.SCENARIOS if s in comparison]
    made = [
        figures.grouped_bars(comparison, scenarios, "availability", "availability (good requests)",
                             "Availability per scenario (mean and standard deviation)",
                             FIGURES / "availability.png", ylim=(0.2, 1.01)),
        figures.grouped_bars(comparison, scenarios, "failed", "failed requests per run (log scale)",
                             "Failed requests per run", FIGURES / "failed_requests.png", log=True),
        figures.grouped_bars(comparison, [s for s in scenarios if s != "E7"], "recovery_s",
                             "seconds from the failure", "Recovery time",
                             FIGURES / "recovery_time.png"),
        diagrams.architecture_baseline(FIGURES / "architecture_baseline.png"),
        diagrams.architecture_ft(FIGURES / "architecture_ft.png"),
        diagrams.rbd(FIGURES / "rbd.png"),
        diagrams.fault_tree_diagram(FIGURES / "fault_tree.png"),
    ]  # fmt: skip
    for scenario in ("E1", "E1b", "E2", "E4", "E6"):
        if any(key[1] == scenario for key in runs):
            marks = ((20, "load rises"), (80, "load falls")) if scenario == "E6" else None
            kwargs = {"marks": marks} if marks else {}
            made.append(
                figures.timeline(runs, scenario, FIGURES / f"timeline_{scenario}.png", **kwargs)
            )
    if any(key[1] == "E7" for key in runs):
        made.append(figures.long_run_timeline(runs, FIGURES / "long_run.png"))
    if "CAL_RUSH" in capacity:
        made.append(figures.capacity_chart(capacity, FIGURES / "capacity.png"))
    print(f"{out}\n" + "\n".join(str(p) for p in made))


if __name__ == "__main__":
    main()
