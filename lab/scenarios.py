"""Loads and validates the scenario definitions in scenarios.toml."""

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


def load(path: Path = SCENARIOS_FILE) -> dict[str, Scenario]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    scenarios = {}
    for name, spec in data["scenario"].items():
        profile = [
            (float(d), float(r)) for d, r in spec.get("rate_profile", defaults["rate_profile"])
        ]
        actions = sorted(spec.get("actions", []), key=lambda a: a["at"])
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
