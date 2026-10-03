"""Figures for the report (PNG, light background).

Colours: the first three slots of a validated categorical palette (checked for colour-vision
deficiency); every chart has a legend and the report repeats the numbers in tables.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from lab.loadgen import read_attempts  # noqa: E402
from lab.metrics import buckets  # noqa: E402

COLORS = {"baseline": "#2a78d6", "sw": "#eb6834", "ft": "#1baf7a"}
LABELS = {
    "baseline": "baseline",
    "sw": "software mechanisms only",
    "ft": "fault-tolerant (software + infrastructure)",
}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def _style(ax) -> None:
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def grouped_bars(
    table: dict,
    scenarios,
    metric: str,
    ylabel: str,
    title: str,
    path: Path,
    *,
    log=False,
    ylim=None,
):
    modes = [m for m in COLORS if any(m in table.get(s, {}) for s in scenarios)]
    width = 0.8 / len(modes)
    fig, ax = plt.subplots(figsize=(10, 4))
    zeros = []
    for i, mode in enumerate(modes):
        xs, ys, errs = [], [], []
        for j, scenario in enumerate(scenarios):
            value = table.get(scenario, {}).get(mode, {}).get(metric, {})
            if value.get("mean") is None:
                continue
            x = j + (i - (len(modes) - 1) / 2) * width
            if log and value["mean"] == 0:
                zeros.append(x)  # nothing to draw on a log scale: labelled "0" instead
                continue
            xs.append(x)
            ys.append(value["mean"])
            errs.append(value["std"])
        ax.bar(xs, ys, width * 0.9, yerr=errs, color=COLORS[mode], label=LABELS[mode],
               error_kw={"ecolor": MUTED, "elinewidth": 1, "capsize": 2})  # fmt: skip
    ax.set_xticks(range(len(scenarios)), scenarios)
    ax.set_ylabel(ylabel, color=INK)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    if log:
        ax.set_yscale("log")
        ax.set_ylim(bottom=0.2)  # means below 1 (a failed request in some runs) stay visible
        for x in zeros:
            ax.text(x, 0.24, "0", ha="center", va="bottom", fontsize=8, color=MUTED)
    if ylim:
        ax.set_ylim(*ylim)
    _style(ax)
    ax.legend(frameon=False, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3)
    return _save(fig, path)


def timeline(runs: dict, scenario: str, path: Path, *, marks=((20, "failure"), (60, "repair"))):
    fig, ax = plt.subplots(figsize=(10, 3.2))
    for mode in COLORS:
        group = runs.get((mode, scenario))
        if not group:
            continue
        run = sorted(group, key=lambda r: r["meta"]["rep"])[0]
        t0 = run["meta"]["t0"]
        duration = sum(d for d, _ in run["meta"]["rate_profile"])
        attempts = read_attempts(run["dir"] / "requests.csv.gz")
        series = buckets(attempts, t0, t0 + duration)
        ax.plot([b.start - t0 for b in series], [b.ratio * 100 for b in series], color=COLORS[mode],
                linewidth=2, label=LABELS[mode])  # fmt: skip
    for at, label in marks:
        ax.axvline(at, color=MUTED, linewidth=1, linestyle="--")
        ax.text(at + 0.5, 3, label, color=MUTED, fontsize=8)
    ax.set_ylim(0, 105)
    ax.set_xlabel("time since start of the run, s", color=INK)
    ax.set_ylabel("good requests in the second, %", color=INK)
    ax.set_title(f"{scenario}: share of good requests per second (repetition 1)", color=INK,
                 fontsize=11, loc="left")  # fmt: skip
    _style(ax)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    return _save(fig, path)


def long_run_timeline(runs: dict, path: Path):
    modes = [m for m in COLORS if runs.get((m, "E7"))]
    fig, axes = plt.subplots(len(modes), 1, figsize=(10, 2.2 * len(modes)), sharex=True)
    axes = axes if len(modes) > 1 else [axes]
    for ax, mode in zip(axes, modes, strict=True):
        run = sorted(runs[(mode, "E7")], key=lambda r: r["meta"]["rep"])[0]
        t0 = run["meta"]["t0"]
        actions = sorted(run["actions"], key=lambda a: a["t"])
        for i, action in enumerate(actions):
            if action["do"] == "repair":
                continue
            end = next((a["t"] for a in actions[i + 1 :] if a["do"] == "repair"), action["t"])
            ax.axvspan(action["t"] - t0, end - t0, color=GRID, linewidth=0)
        attempts = read_attempts(run["dir"] / "requests.csv.gz")
        series = buckets(attempts, t0, t0 + 1200)
        ax.plot([b.start - t0 for b in series], [b.ratio * 100 for b in series],
                color=COLORS[mode], linewidth=1.2)  # fmt: skip
        ax.set_ylim(0, 105)
        ax.set_ylabel("good, %", color=INK)
        ax.set_title(LABELS[mode], color=INK, fontsize=10, loc="left")
        _style(ax)
    axes[-1].set_xlabel("time, s (grey: a failure is active until its repair)", color=INK)
    fig.suptitle("E7: long run with random failures (repetition 1)", x=0.01, ha="left",
                 color=INK, fontsize=11)  # fmt: skip
    return _save(fig, path)


def capacity_chart(capacity: dict, path: Path):
    fig, ax = plt.subplots(figsize=(6, 3.4))
    for mode, steps in capacity.get("CAL_RUSH", {}).items():
        ax.plot([s["rate"] for s in steps], [s["availability"] * 100 for s in steps],
                color=COLORS[mode], linewidth=2, marker="o", markersize=5,
                label=LABELS[mode])  # fmt: skip
    ax.set_xlabel("offered load, requests per second (registration rush mix)", color=INK)
    ax.set_ylabel("good requests, %", color=INK)
    ax.set_ylim(0, 105)
    ax.set_title("Capacity: share of good requests per load step", color=INK, fontsize=11,
                 loc="left")  # fmt: skip
    _style(ax)
    ax.legend(frameon=False, fontsize=9, loc="lower left")
    return _save(fig, path)
