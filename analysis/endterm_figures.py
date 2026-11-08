"""Figures of the endterm report: availability comparison and the monitoring view of a run
(what Prometheus saw and which alerts fired), redrawn from the stored time series."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from analysis.figures import COLORS, GRID, INK, LABELS, MUTED, _save, _style  # noqa: E402

MARKED_ACTIONS = {
    "crash", "hang", "kill", "toxic", "flood", "login_attack", "probe_unauthorized",
    "probe_malformed", "repair",
}  # fmt: skip
COLORS = {**COLORS, "ft2": "#8a4fd6"}
LABELS = {
    **LABELS,
    "ft": "midterm configuration",
    "ft2": "endterm configuration",
}


def availability_bars(summary: dict, scenarios, path: Path) -> Path:
    modes = [
        m for m in ("baseline", "ft", "ft2") if any(m in summary.get(s, {}) for s in scenarios)
    ]
    width = 0.8 / len(modes)
    fig, ax = plt.subplots(figsize=(10, 3.8))
    for i, mode in enumerate(modes):
        xs, ys, errs = [], [], []
        for j, scenario in enumerate(scenarios):
            value = summary.get(scenario, {}).get(mode, {}).get("availability", {})
            if value.get("mean") is None:
                continue
            xs.append(j + (i - (len(modes) - 1) / 2) * width)
            ys.append(value["mean"] * 100)
            errs.append(value["std"] * 100)
        ax.bar(xs, ys, width * 0.9, yerr=errs, color=COLORS[mode], label=LABELS[mode],
               error_kw={"ecolor": MUTED, "elinewidth": 1, "capsize": 2})  # fmt: skip
    ax.set_xticks(range(len(scenarios)), scenarios)
    ax.set_ylabel("good requests, %", color=INK)
    ax.set_ylim(50, 100.5)
    ax.set_title("Availability per scenario (mean and standard deviation)", color=INK,
                 fontsize=11, loc="left")  # fmt: skip
    _style(ax)
    ax.legend(frameon=False, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3)
    return _save(fig, path)


def monitoring_view(run_dir: Path, title: str, path: Path) -> Path:
    """Error ratio, healthy replicas and the alerts that fired, with the injected faults."""
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    alerts = json.loads((run_dir / "alerts.json").read_text(encoding="utf-8"))
    actions = json.loads((run_dir / "actions.json").read_text(encoding="utf-8"))
    t0 = meta["t0"]
    series = alerts["series"]
    names = sorted({a["alert"] for a in alerts["firing"] if a["to"] >= t0 - 1})
    fig, (top, mid, low) = plt.subplots(
        3, 1, figsize=(10, 6.2), sharex=True,
        gridspec_kw={"height_ratios": [2, 2, max(len(names), 1) * 0.55 + 0.6]},
    )  # fmt: skip

    def xy(name, scale=1.0):
        return [t - t0 for t, _ in series[name]], [v * scale for _, v in series[name]]

    top.plot(*xy("error_ratio", 100), color=COLORS["ft2"], linewidth=2, label="5xx share, %")
    top.set_ylabel("5xx share, %", color=INK)
    top.set_ylim(bottom=0)
    twin = top.twinx()
    twin.plot(*xy("requests_per_s"), color=MUTED, linewidth=1, label="requests per second")
    twin.set_ylabel("requests per second", color=MUTED)
    twin.tick_params(colors=MUTED, labelsize=9)
    mid.plot(*xy("healthy_replicas"), color=COLORS["ft"], linewidth=2, label="healthy replicas")
    mid.plot(*xy("targets_up"), color=COLORS["baseline"], linewidth=1, label="metric targets up")
    mid.plot(*xy("primary"), color=COLORS["sw"], linewidth=1, label="database primaries")
    mid.legend(frameon=False, fontsize=8, loc="lower right")
    mid.set_ylabel("count", color=INK)
    for row, name in enumerate(names):
        for alert in alerts["firing"]:
            if alert["alert"] == name:
                low.barh(row, alert["to"] - alert["from"] + 2, left=alert["from"] - t0,
                         height=0.6, color=COLORS["ft2"], alpha=0.85)  # fmt: skip
    low.set_yticks(range(len(names)), names, fontsize=8)
    low.set_ylim(-0.6, max(len(names), 1) - 0.4)
    low.invert_yaxis()
    low.set_xlabel("time since start of the run, s", color=INK)
    for ax in (top, mid, low):
        _style(ax)
        for record in actions:
            if record.get("do") in MARKED_ACTIONS:
                ax.axvline(record["t"] - t0, color=MUTED, linewidth=0.8, linestyle="--")
    low.xaxis.grid(False)
    top.set_title(title, color=INK, fontsize=11, loc="left")
    low.yaxis.grid(True, color=GRID, linewidth=0.8)
    fig.tight_layout()
    return _save(fig, path)
