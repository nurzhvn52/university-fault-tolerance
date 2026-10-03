"""Loads and validates the scenario definitions in scenarios.toml.

A scenario either lists its actions, or has a ``generator`` table that produces them from the
seed of the run (the long run E7): an alternating renewal process in which up times and
repair times are exponentially distributed and every failure is drawn from a weighted pool.
The same seed gives the same failure schedule to every version of the system.
"""

import random
import tomllib
from dataclasses import dataclass
from pathlib import Path

from lab.actions import ActionRunner
from lab.workload import SERVICE_OF

SCENARIOS_FILE = Path(__file__).with_name("scenarios.toml")


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    rate_profile: list[tuple[float, float]]
    actions: list[dict]
    fault_at: float | None
    # Operation weights of the load; None means the default mix of lab.workload.
    mix: dict[str, int] | None = None

    @property
    def duration(self) -> float:
        return sum(duration for duration, _ in self.rate_profile)

    @property
    def expected_batch(self) -> int | None:
        sizes = [a["size"] for a in self.actions if a["do"] == "grade_batch"]
        return sizes[0] if sizes else None


def generate_failures(generator: dict, duration: float, seed: int) -> list[dict]:
    """Up time ~ Exp(mean_up_s), then a failure from the pool, repaired after
    ~ Exp(mean_repair_s) (bounded); the next up time starts after the repair."""
    rng = random.Random(seed)
    pool = generator["faults"]
    weights = [fault.get("weight", 1) for fault in pool]
    actions = []
    t = generator.get("warmup_s", 30)
    while True:
        t += max(rng.expovariate(1 / generator["mean_up_s"]), 5)
        repair = min(max(rng.expovariate(1 / generator["mean_repair_s"]), 5), 120)
        if t + repair > duration - generator.get("cooldown_s", 30):
            break
        fault = dict(rng.choices(pool, weights)[0])
        fault.pop("weight", None)
        for key, value in fault.items():
            if isinstance(value, list):
                fault[key] = rng.choice(value)
        actions.append({"at": round(t, 1), **fault})
        actions.append({"at": round(t + repair, 1), "do": "repair"})
        t += repair
    return actions


def load(path: Path = SCENARIOS_FILE, seed: int = 1) -> dict[str, Scenario]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    scenarios = {}
    for name, spec in data["scenario"].items():
        profile = [
            (float(d), float(r)) for d, r in spec.get("rate_profile", defaults["rate_profile"])
        ]
        actions = spec.get("actions", [])
        if "generator" in spec:
            duration = sum(d for d, _ in profile)
            actions = generate_failures(spec["generator"], duration, seed)
        actions = sorted(actions, key=lambda a: a["at"])
        fault_at = spec.get("fault_at", actions[0]["at"] if actions else None)
        scenario = Scenario(
            name,
            spec["title"],
            spec.get("description", ""),
            profile,
            actions,
            fault_at,
            spec.get("mix"),
        )
        _validate(scenario)
        scenarios[name] = scenario
    return scenarios


def _validate(scenario: Scenario) -> None:
    unknown = set(scenario.mix or {}) - set(SERVICE_OF)
    if unknown:
        raise ValueError(f"{scenario.name}: unknown operations in mix: {sorted(unknown)}")
    for action in scenario.actions:
        if not hasattr(ActionRunner, "do_" + action["do"]):
            raise ValueError(f"{scenario.name}: unknown action {action['do']!r}")
        if not 0 <= action["at"] < scenario.duration:
            raise ValueError(f"{scenario.name}: action at {action['at']} s is outside the run")
