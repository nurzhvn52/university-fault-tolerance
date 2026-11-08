"""Builds the evaluation of the endterm result set.

    python -m analysis.run_endterm --tag final

Writes results/<tag>/endterm_analysis.json and the figures in docs/figures/.
"""

import argparse
from pathlib import Path

from analysis import endterm, endterm_figures, results
from analysis.endterm_diagrams import architecture_endterm

ROOT = Path(__file__).resolve().parent.parent
FIGURES = ROOT / "docs" / "figures"
RESILIENCE = ("X1", "X1b", "X2", "X3", "X4", "X6")
MONITORED = {"X1": "crash", "X1b": "hang", "X2": "timeout", "X3": "database", "X5": "alert_test"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="final")
    args = parser.parse_args()

    runs = results.load_runs(args.tag)
    summary = endterm.summarize(runs)
    analysis = {
        "summary": summary,
        "alert_rules": endterm.alert_catalog(ROOT / "infra" / "monitoring" / "rules.yml"),
    }
    out = ROOT / "results" / args.tag / "endterm_analysis.json"
    endterm.dump(out, analysis)

    made = [
        architecture_endterm(FIGURES / "architecture_endterm.png"),
        endterm_figures.availability_bars(
            summary, [s for s in RESILIENCE if s in summary], FIGURES / "endterm_availability.png"
        ),
    ]
    for scenario, name in MONITORED.items():
        group = runs.get(("ft2", scenario))
        if group:
            run = sorted(group, key=lambda r: r["meta"]["rep"])[0]
            made.append(
                endterm_figures.monitoring_view(
                    run["dir"],
                    f"{scenario} ({name}): what the monitoring saw (repetition 1)",
                    FIGURES / f"monitoring_{scenario}.png",
                )
            )
    print(f"{out}\n" + "\n".join(str(p) for p in made))


if __name__ == "__main__":
    main()
