"""Architecture diagram of the endterm configuration (ft2)."""

from pathlib import Path

from analysis.diagrams import ACCENT, MUTED, _box, _canvas, _line, _node, _save

ENDTERM_SERVICES = ["auth", "student", "payment", "records", "timetable"]


def architecture_endterm(path: Path) -> Path:
    fig, ax = _canvas(12.4, 7.6)
    client = _box(ax, 5.2, 7.1, 2.0, 0.4, "clients (load generator)")
    gw = _box(
        ax,
        3.6,
        6.0,
        5.2,
        0.75,
        "gateway (HAProxy, node edge)\nrequest id, health checks, retries, 256 KiB limit,\n"
        "login rate limit, no routes to internal endpoints",
    )
    _line(ax, client.bottom, gw.top)
    for node, x in (("a", 0.2), ("b", 6.4)):
        n = 1 if node == "a" else 2
        _node(ax, x, 2.75, 5.8, 2.95, f"node {node}")
        for i, name in enumerate(ENDTERM_SERVICES):
            service = _box(ax, x + 0.15 + i * 1.1, 4.45, 1.0, 0.5, f"{name}-{n}", size=7)
            _line(ax, gw.bottom, service.top)
        ax.text(
            x + 0.15,
            3.95,
            "JWT checked locally in every service; roles and ownership;\n"
            "rate limit per principal; audit events; service role in the database",
            fontsize=7,
            color=MUTED,
            va="top",
        )
        ax.text(
            x + 0.15, 3.1, "restart policy, watchdog, metrics at /metrics", fontsize=7, color=MUTED
        )
    pg1 = _box(ax, 0.9, 1.55, 2.8, 0.55, "pg-1 (Patroni primary)\naudit.events, role per service")
    pg2 = _box(ax, 4.7, 1.55, 2.4, 0.55, "pg-2 (Patroni standby)")
    _line(ax, pg1.right, pg2.left, color=ACCENT, lw=1.5)
    bank = _box(ax, 9.0, 1.55, 2.4, 0.55, "bank (external)\nAPI key only")
    _line(ax, (3.0, 2.75), pg1.top)
    _line(ax, (10.2, 2.75), bank.top)
    _node(ax, 0.2, 0.05, 11.8, 1.1, "monitoring (node c)")
    prom = _box(ax, 0.6, 0.2, 2.5, 0.55, "Prometheus\n11 alert rules, scrape every 2 s")
    am = _box(ax, 4.0, 0.2, 2.0, 0.55, "Alertmanager")
    sink = _box(ax, 6.9, 0.2, 2.0, 0.55, "alert sink\n(notification times)")
    _box(ax, 9.6, 0.2, 2.0, 0.55, "Grafana dashboard")
    _line(ax, prom.right, am.left)
    _line(ax, am.right, sink.left)
    _line(ax, (0.45, 1.15), (0.45, 2.75), arrow=False, ls=":")
    ax.text(
        0.2,
        7.35,
        "secrets from files (JWT key, bank key)",
        fontsize=7,
        color=MUTED,
    )
    return _save(fig, path)
