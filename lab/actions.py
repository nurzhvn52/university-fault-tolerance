"""Actions on a scenario timeline: failure injection, repair and client operations.

Targets:
- a compose service name, e.g. ``payment-1``;
- ``service:<name>``: every instance of a logical service (label ``uft.service``);
- ``db-primary``: the current primary database.

``repair`` models a person fixing the system: it starts every stopped container of the stack
(whatever stopped it: the lab, a crash, a failed node), unpauses paused ones and removes all
network faults. Both versions of the system get the same repair actions at the same times.
"""

import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from lab import docker_ctl
from lab.workload import TERM

logger = logging.getLogger("lab.actions")

GRADE_BATCH_TERM = "2026-IMPORT"
# Containers that are meant to exit (migrations) or are the lab itself.
ONE_SHOT_ROLES = {"job", "lab"}
LETTERS = ["A", "A-", "B+", "B", "B-", "C+", "C"]


@dataclass
class LabContext:
    project: str
    http: httpx.AsyncClient
    gateway_url: str
    toxiproxy_url: str
    seed: int


@dataclass
class ActionRunner:
    ctx: LabContext
    records: list[dict] = field(default_factory=list)

    async def execute(self, action: dict, offset: float) -> None:
        record = {"t": time.time(), "at": offset, **action}
        try:
            handler = getattr(self, "do_" + action["do"])
            params = {key: value for key, value in action.items() if key not in ("do", "at")}
            record["result"] = await handler(**params)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("action_failed", extra={"action": action})
        self.records.append(record)

    async def resolve(self, target: str) -> list[docker_ctl.Container]:
        all_containers = await docker_ctl.containers(self.ctx.project)
        if target == "db-primary":
            found = [c for c in all_containers if c.labels.get("uft.role") == "db"]
            if len(found) != 1:
                raise ValueError(f"expected one database container, found {len(found)}")
        elif target.startswith("service:"):
            name = target.removeprefix("service:")
            found = [c for c in all_containers if c.labels.get("uft.service") == name]
        else:
            found = [c for c in all_containers if c.service == target]
        if not found:
            raise ValueError(f"no container for target {target!r}")
        return found

    async def do_kill(self, target: str) -> list[str]:
        names = [c.name for c in await self.resolve(target) if c.running]
        for name in names:
            await docker_ctl.kill(name)
        return names

    async def do_stop(self, target: str) -> list[str]:
        names = [c.name for c in await self.resolve(target) if c.running]
        for name in names:
            await docker_ctl.stop(name)
        return names

    async def do_pause(self, target: str) -> list[str]:
        names = [c.name for c in await self.resolve(target) if c.running]
        for name in names:
            await docker_ctl.pause(name)
        return names

    async def do_kill_node(self, node: str) -> list[str]:
        containers = await docker_ctl.containers(self.ctx.project)
        names = [c.name for c in containers if c.labels.get("uft.node") == node and c.running]
        for name in names:
            await docker_ctl.kill(name)
        return names

    async def do_toxic(self, proxy: str, toxic: str, attributes: dict) -> dict:
        response = await self.ctx.http.post(
            f"{self.ctx.toxiproxy_url}/proxies/{proxy}/toxics",
            json={"name": f"{proxy}_{toxic}", "type": toxic, "attributes": attributes},
        )
        response.raise_for_status()
        return response.json()

    async def do_arm_fault(self, target: str, point: str, action: str, **options) -> list[str]:
        armed = []
        for container in await self.resolve(target):
            response = await self.ctx.http.post(
                f"http://{container.service}:8000/_chaos/faults",
                json={"point": point, "action": action, **options},
            )
            response.raise_for_status()
            armed.append(container.name)
        return armed

    async def do_repair(self) -> dict:
        containers = [
            c
            for c in await docker_ctl.containers(self.ctx.project)
            if c.labels.get("uft.role") not in ONE_SHOT_ROLES
        ]
        unpaused = sorted(c.name for c in containers if c.state == "paused")
        started = sorted(c.name for c in containers if c.state in ("exited", "dead"))
        for name in unpaused:
            await docker_ctl.unpause(name)
        for name in started:
            await docker_ctl.start(name)
        response = await self.ctx.http.post(f"{self.ctx.toxiproxy_url}/reset")
        response.raise_for_status()
        return {"started": started, "unpaused": unpaused, "toxics_reset": True}

    async def do_grade_batch(self, size: int) -> dict:
        """Import a batch of grades through the gateway. Repeating the action sends the same
        batch again, like an operator re-running a failed import."""
        rng = random.Random(self.ctx.seed)
        items = [
            {
                "student_id": student_id,
                "course_code": "CSE316",
                "term": GRADE_BATCH_TERM,
                "letter": rng.choice(LETTERS),
                "credits": 5,
            }
            for student_id in range(1, size + 1)
        ]
        return await self._call("POST", "/api/grades/batch", json={"items": items})

    async def do_generate_timetable(self) -> dict:
        return await self._call("POST", "/api/timetable/generate", params={"term": TERM})

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = await self.ctx.http.request(
                method, f"{self.ctx.gateway_url}{path}", timeout=30, **kwargs
            )
        except httpx.HTTPError as exc:
            return {"status": 0, "error": type(exc).__name__}
        return {"status": response.status_code, "body": response.text[:500]}
