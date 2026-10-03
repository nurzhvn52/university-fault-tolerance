"""Theoretical reliability model of the portal: reliability block diagrams and a fault tree.

Formulas (course notation, Week 1-2): A = MTTF / (MTTF + MTTR); series blocks multiply
availabilities, a parallel pair is 1 - (1 - A1)(1 - A2); R(t) = exp(-t / MTTF) for a constant
failure rate. Fault tree: an OR gate fails if any input fails, P = 1 - prod(1 - p_i); an AND
gate fails only if all inputs fail, P = prod(p_i). Inputs are assumed independent.

The component parameters below are illustrative assumptions for a university portal (order
of magnitude, not measurements); the experiments supply the measured lab-scale counterparts.
"""

import math
from dataclasses import dataclass

HOURS_PER_YEAR = 8760


def availability(mttf_h: float, mttr_h: float) -> float:
    return mttf_h / (mttf_h + mttr_h)


def series(*blocks: float) -> float:
    return math.prod(blocks)


def parallel(*blocks: float) -> float:
    return 1 - math.prod(1 - a for a in blocks)


def reliability(t_h: float, mttf_h: float) -> float:
    return math.exp(-t_h / mttf_h)


def downtime_min_per_year(a: float) -> float:
    return (1 - a) * HOURS_PER_YEAR * 60


@dataclass(frozen=True)
class Component:
    name: str
    mttf_h: float
    mttr_manual_h: float  # a person notices, diagnoses and repairs
    mttr_auto_h: float  # the fault-tolerant version recovers by itself


# Assumptions (documented in the report).
GATEWAY = Component("gateway (HAProxy)", mttf_h=2160, mttr_manual_h=1.0, mttr_auto_h=1.0)
NODE = Component("node (server / VM)", mttf_h=4380, mttr_manual_h=4.0, mttr_auto_h=4.0)
APP = Component("service instance", mttf_h=720, mttr_manual_h=1.0, mttr_auto_h=1 / 60)
DATABASE = Component("database server", mttf_h=2160, mttr_manual_h=2.0, mttr_auto_h=2.0)
FAILOVER_S = 25  # measured Patroni failover time (E2, FT)
SERVICES = ("student", "payment", "records", "timetable")


def baseline_model() -> dict:
    """Everything single: gateway, node, four services and the database in series."""
    blocks = {
        "gateway": availability(GATEWAY.mttf_h, GATEWAY.mttr_manual_h),
        "node": availability(NODE.mttf_h, NODE.mttr_manual_h),
        **{s: availability(APP.mttf_h, APP.mttr_manual_h) for s in SERVICES},
        "database": availability(DATABASE.mttf_h, DATABASE.mttr_manual_h),
    }
    return {"blocks": blocks, "availability": series(*blocks.values())}


def ft_model() -> dict:
    """Gateway single; two nodes, two instances per service and two database nodes in
    parallel pairs. A database failure also costs one failover outage."""
    a_app = availability(APP.mttf_h, APP.mttr_auto_h)
    a_db = availability(DATABASE.mttf_h, DATABASE.mttr_manual_h)
    failover_unavailability = (FAILOVER_S / 3600) / DATABASE.mttf_h
    blocks = {
        "gateway": availability(GATEWAY.mttf_h, GATEWAY.mttr_manual_h),
        "node pair": parallel(*[availability(NODE.mttf_h, NODE.mttr_manual_h)] * 2),
        **{f"{s} pair": parallel(a_app, a_app) for s in SERVICES},
        "database pair": parallel(a_db, a_db) * (1 - failover_unavailability),
    }
    return {"blocks": blocks, "availability": series(*blocks.values())}


def fault_tree(model: dict) -> dict:
    """Top event: the portal is unavailable (OR of its blocks, each the AND of its
    redundant parts). Returns P(top) and the blocks as minimal-cut-set orders."""
    unavailability = {name: 1 - a for name, a in model["blocks"].items()}
    p_top = 1 - math.prod(1 - q for q in unavailability.values())
    cut_set_order = {name: (2 if "pair" in name else 1) for name in unavailability}
    return {"p_top": p_top, "events": unavailability, "cut_set_order": cut_set_order}


def sensitivity(mttf_h: float, mttr_h: float, factor: float = 4) -> dict:
    """Week 2 question: improve the time between failures or the repair time?"""
    base = availability(mttf_h, mttr_h)
    return {
        "base": base,
        f"mttf_x{factor:g}": availability(mttf_h * factor, mttr_h),
        f"mttr_div{factor:g}": availability(mttf_h, mttr_h / factor),
    }


def summary() -> dict:
    baseline, ft = baseline_model(), ft_model()
    return {
        "assumptions": {
            c.name: {
                "MTTF_h": c.mttf_h,
                "MTTR_manual_h": c.mttr_manual_h,
                "MTTR_auto_h": c.mttr_auto_h,
            }
            for c in (GATEWAY, NODE, APP, DATABASE)
        }
        | {"failover_s": FAILOVER_S},
        "baseline": {
            **baseline,
            "downtime_min_per_year": downtime_min_per_year(baseline["availability"]),
            "fault_tree": fault_tree(baseline),
        },
        "ft": {
            **ft,
            "downtime_min_per_year": downtime_min_per_year(ft["availability"]),
            "fault_tree": fault_tree(ft),
        },
        "reliability_30_days": {
            "single instance": reliability(720, APP.mttf_h),
            # A pair is down only if both instances fail within one repair window, so its
            # MTTF is about MTTF^2 / (2 * MTTR) (standard result for a repairable pair).
            "instance pair": reliability(720, APP.mttf_h**2 / (2 * APP.mttr_auto_h)),
        },
        "sensitivity_database": sensitivity(DATABASE.mttf_h, DATABASE.mttr_manual_h),
        "sensitivity_week2_example": sensitivity(500, 20),
    }
