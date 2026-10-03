"""Diagrams for the report drawn with matplotlib: architecture, reliability block diagrams
and the fault tree. Connectors run between box edges, never through labels."""

from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

from analysis.model import SERVICES, fault_tree, ft_model  # noqa: E402

INK, MUTED, FILL, NODE_FILL = "#0b0b0b", "#52514e", "#f2f1ed", "#fbfaf7"
ACCENT = "#2a78d6"


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def top(self):
        return (self.x + self.w / 2, self.y + self.h)

    @property
    def bottom(self):
        return (self.x + self.w / 2, self.y)

    @property
    def left(self):
        return (self.x, self.y + self.h / 2)

    @property
    def right(self):
        return (self.x + self.w, self.y + self.h / 2)


def _canvas(width: float, height: float):
    fig, ax = plt.subplots(figsize=(width, height))
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.axis("off")
    return fig, ax


def _box(ax, x, y, w, h, text, *, fill=FILL, size=8) -> Box:
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02", facecolor=fill,
                                edgecolor=MUTED, linewidth=1))  # fmt: skip
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color=INK)
    return Box(x, y, w, h)


def _node(ax, x, y, w, h, title) -> None:
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02", facecolor=NODE_FILL,
                                edgecolor=MUTED, linewidth=1, linestyle="--"))  # fmt: skip
    ax.text(x + 0.08, y + h - 0.06, title, fontsize=8, color=MUTED, va="top")


def _line(ax, start, end, *, arrow=True, color=MUTED, lw=1.0, ls="-") -> None:
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>" if arrow else "-",
                                 mutation_scale=9, color=color, linewidth=lw, linestyle=ls,
                                 shrinkA=1, shrinkB=1))  # fmt: skip


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def architecture_baseline(path: Path) -> Path:
    fig, ax = _canvas(10, 4.4)
    client = _box(ax, 4.0, 3.85, 2.0, 0.4, "clients (load generator)")
    _node(ax, 0.3, 0.2, 7.4, 3.35, "node a (one server)")
    gw = _box(ax, 3.9, 2.65, 2.2, 0.5, "gateway (HAProxy)\nrouting only")
    db = _box(ax, 3.6, 0.45, 2.8, 0.45, "PostgreSQL (single)")
    _line(ax, client.bottom, gw.top)
    for i, name in enumerate(SERVICES):
        service = _box(ax, 0.6 + i * 1.75, 1.5, 1.5, 0.45, name)
        _line(ax, gw.bottom, service.top)
        _line(ax, service.bottom, db.top)
    _box(ax, 8.1, 1.5, 1.7, 0.6, "bank (external)\ncalled by payment")
    note = (
        "No replicas, no health checks, no restart policy; toxiproxy (not shown) sits on the "
        "service-to-service paths for network faults."
    )
    ax.text(0.3, -0.1, note, fontsize=8, color=MUTED)
    return _save(fig, path)


def architecture_ft(path: Path) -> Path:
    fig, ax = _canvas(12, 6.6)
    client = _box(ax, 5.0, 6.05, 2.0, 0.4, "clients (load generator)")
    gw = _box(
        ax, 4.5, 5.1, 3.0, 0.55, "gateway (HAProxy, node edge)\nhealth checks, retries, DB routing"
    )
    _line(ax, client.bottom, gw.top)
    pg = {}
    for node, x in (("a", 0.2), ("b", 6.1)):
        n = 1 if node == "a" else 2
        _node(ax, x, 1.3, 5.7, 3.4, f"node {node}")
        for i, name in enumerate(SERVICES):
            service = _box(ax, x + 0.2 + i * 1.37, 3.6, 1.25, 0.45, f"{name}-{n}")
            _line(ax, gw.bottom, service.top)
        ax.text(
            x + 0.2, 3.2, "restart policy and watchdog for every service", fontsize=7, color=MUTED
        )
        # The database nodes face each other so the replication arrow crosses nothing.
        pg_x, etcd_x = (x + 2.9, x + 0.3) if node == "a" else (x + 0.2, x + 3.8)
        role = "Patroni primary" if node == "a" else "Patroni standby"
        pg[node] = _box(ax, pg_x, 1.8, 2.6, 0.6, f"pg-{n}\n{role}")
        etcd = _box(ax, etcd_x, 1.8, 1.6, 0.6, f"etcd-{n}")
        _line(ax, etcd.bottom, (6.0, 0.95), arrow=False, ls=":")
    _line(ax, pg["a"].right, pg["b"].left, color=ACCENT, lw=1.5)
    ax.text(6.0, 2.5, "synchronous\nreplication", fontsize=7, color=ACCENT, ha="center")
    _node(ax, 3.2, 0.05, 5.6, 0.95, "node c")
    _box(ax, 3.4, 0.12, 1.3, 0.5, "etcd-3")
    _box(ax, 4.9, 0.12, 1.2, 0.5, "watchdog")
    _box(ax, 6.3, 0.12, 1.1, 0.5, "backup")
    _box(ax, 7.55, 0.12, 1.15, 0.5, "monitoring")
    _box(
        ax,
        0.2,
        0.12,
        2.8,
        0.6,
        "transcript disks 1-3 (mirrored)\nshared by records-1 and -2",
        size=7,
    )
    _box(ax, 9.2, 0.12, 2.6, 0.6, "bank (external)\ncalled by payment-1 and -2", size=7)
    return _save(fig, path)


def rbd(path: Path) -> Path:
    fig, ax = _canvas(12, 3.6)
    ax.text(0.1, 3.45, "Baseline: every block in series", fontsize=9, color=INK, va="top")
    previous = None
    for i, name in enumerate(["gateway", "node", *SERVICES, "database"]):
        box = _box(ax, 0.3 + i * 1.6, 2.4, 1.35, 0.5, name)
        if previous:
            _line(ax, previous.right, box.left, arrow=False)
        previous = box
    ax.text(0.1, 1.8, "Fault-tolerant: series of parallel pairs (the gateway stays single)",
            fontsize=9, color=INK, va="top")  # fmt: skip
    blocks = [("gateway", None), ("node a", "node b")]
    blocks += [(f"{s}-1", f"{s}-2") for s in SERVICES] + [("pg-1", "pg-2")]
    mid = 0.69
    previous_right = None
    for i, (top, bottom) in enumerate(blocks):
        x = 0.3 + i * 1.6
        if bottom is None:
            _box(ax, x, mid - 0.25, 1.35, 0.5, top)
            left, right = x, x + 1.35
        else:
            left, right = x - 0.08, x + 1.43
            for y, label in ((0.85, top), (0.1, bottom)):
                box = _box(ax, x, y, 1.35, 0.42, label, size=7)
                cy = box.y + box.h / 2
                ax.plot([left, left, x], [mid, cy, cy], color=MUTED, linewidth=1)
                ax.plot([x + 1.35, right, right], [cy, cy, mid], color=MUTED, linewidth=1)
        if previous_right is not None:
            ax.plot([previous_right, left], [mid, mid], color=MUTED, linewidth=1)
        previous_right = right
    return _save(fig, path)


def fault_tree_diagram(path: Path) -> Path:
    tree = fault_tree(ft_model())
    fig, ax = _canvas(12, 4.6)
    top = _box(
        ax, 4.6, 3.85, 2.8, 0.6, f"portal unavailable\nP = {tree['p_top']:.2e}", fill="#fde2dd"
    )
    ax.text(6.0, 3.55, "OR", ha="center", va="center", fontsize=9, color=INK, fontweight="bold")
    _line(ax, top.bottom, (6.0, 3.68), arrow=False)
    events = list(tree["events"].items())
    width = 12 / len(events)
    for i, (name, q) in enumerate(events):
        cx = i * width + 0.1
        if "pair" in name:
            base = name.replace(" pair", "")
            child = _box(ax, cx, 2.1, width - 0.2, 0.7, f"{base} unavailable\nq = {q:.1e}", size=7)
            gate_y = 1.75
            ax.text(child.bottom[0], gate_y, "AND", ha="center", va="center", fontsize=8,
                    color=INK, fontweight="bold")  # fmt: skip
            _line(ax, child.bottom, (child.bottom[0], gate_y + 0.12), arrow=False)
            leaves = {"node": ("node a", "node b"), "database": ("pg-1", "pg-2")}.get(
                base, (f"{base}-1", f"{base}-2")
            )
            for k, leaf_name in enumerate(leaves):
                leaf = _box(
                    ax, cx + k * (width - 0.2) / 2, 0.55, (width - 0.3) / 2, 0.55, leaf_name, size=6
                )
                _line(ax, (child.bottom[0], gate_y - 0.12), leaf.top, arrow=False)
        else:
            child = _box(
                ax, cx, 2.1, width - 0.2, 0.7, f"{name}\nq = {q:.1e}\n(basic event)", size=7
            )
        _line(ax, (6.0, 3.42), child.top, arrow=False)
    note = (
        "q: probability that the block is unavailable (1 - A). Basic events are independent "
        "instance failures; the gateway is the only first-order minimal cut set left. "
        "Baseline: OR of seven single basic events."
    )
    ax.text(0.1, 0.1, note, fontsize=8, color=MUTED, wrap=True)
    return _save(fig, path)
