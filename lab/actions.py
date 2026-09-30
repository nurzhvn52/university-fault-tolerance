"""Actions on a scenario timeline: failure injection, repair and client operations.

Targets:
- a compose service name, e.g. ``payment-1``;
- ``service:<name>``: every instance of a logical service (label ``uft.service``);
- ``db-primary``: the current primary database (with several database nodes, the one
  Patroni reports as primary).

``repair`` models a person fixing the system: it starts every stopped container of the stack
(whatever stopped it: the lab, a crash, a failed node), unpauses paused ones, restarts services
the lab made hang if they still do not answer, and removes all network faults. Both versions
of the system get the same repair actions at the same times.
"""

import contextlib
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

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
    _hung: dict[str, str] = field(default_factory=dict)  # container name -> compose service

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
            found = await self._db_primary(
                [c for c in all_containers if c.labels.get("uft.role") == "db"]
            )
        elif target.startswith("service:"):
            name = target.removeprefix("service:")
            found = [c for c in all_containers if c.labels.get("uft.service") == name]
        else:
            found = [c for c in all_containers if c.service == target]
        if not found:
            raise ValueError(f"no container for target {target!r}")
        return found

    async def _db_primary(self, nodes: list) -> list:
        if len(nodes) <= 1:
            return nodes
        for node in nodes:
            if not node.running:
                continue
            try:
                response = await self.ctx.http.get(f"http://{node.service}:8008/", timeout=2)
            except httpx.HTTPError:
                continue
            if response.json().get("role") in ("primary", "master"):
                return [node]
        raise ValueError("no database node reports itself as primary")

    async def do_crash(self, target: str) -> list[str]:
        """The service process dies (exit code 137). Unlike ``docker kill`` this is not a
        manual stop, so a restart policy brings the container back."""
        crashed = []
        for container in await self.resolve(target):
            if not container.running:
                continue
            # The connection dies with the process, so an error is the expected answer.
            with contextlib.suppress(httpx.HTTPError):
                await self.ctx.http.post(f"http://{container.service}:8000/_chaos/crash", timeout=2)
            crashed.append(container.name)
        return crashed

    async def do_hang(self, target: str, seconds: float = 3600) -> list[str]:
        """The service process stops answering but stays alive."""
        hung = []
        for container in await self.resolve(target):
            # A hung process answers nothing; the request times out.
            with contextlib.suppress(httpx.HTTPError):
                await self.ctx.http.post(
                    f"http://{container.service}:8000/_chaos/hang",
                    params={"seconds": seconds},
                    timeout=0.5,
                )
            hung.append(container.name)
            self._hung[container.name] = container.service
        return hung

    async def do_corrupt_disk(self, disk: int) -> dict:
        """Flips one byte in every transcript file on one disk (silent data corruption)."""
        root = self._disk(disk)
        if root is None:
            return {"disk": disk, "files": 0, "note": "no such disk"}
        files = [p for p in root.rglob("*.json") if p.is_file()]
        for path in files:
            data = bytearray(path.read_bytes())
            data[len(data) // 2] ^= 0xFF
            path.write_bytes(bytes(data))
        return {"disk": str(root), "files": len(files)}

    async def do_wipe_disk(self, disk: int) -> dict:
        """Deletes every file on one disk (a failed disk replaced by an empty one)."""
        root = self._disk(disk)
        if root is None:
            return {"disk": disk, "files": 0, "note": "no such disk"}
        files = [p for p in root.rglob("*") if p.is_file()]
        for path in files:
            path.unlink()
        return {"disk": str(root), "files": len(files)}

    @staticmethod
    def _disk(disk: int) -> Path | None:
        roots = [r for r in os.environ.get("UFT_TRANSCRIPT_DIRS", "").split(",") if r]
        return Path(roots[disk - 1]) if 0 < disk <= len(roots) else None

    async def do_create_documents(self, count: int) -> dict:
        statuses = []
        for student_id in range(2, 2 + count):
            result = await self._call("POST", f"/api/transcripts/{student_id}/documents")
            statuses.append(result["status"])
        return {"created": statuses.count(201), "statuses": sorted(set(statuses))}

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

    async def do_disarm(self, target: str) -> list[str]:
        """Clears the armed fault points (before a client repeats an interrupted operation)."""
        cleared = []
        for container in await self.resolve(target):
            if not container.running:
                continue
            with contextlib.suppress(httpx.HTTPError):
                await self.ctx.http.delete(f"http://{container.service}:8000/_chaos/faults")
                cleared.append(container.name)
        return cleared

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
        restarted = []
        for name, service in self._hung.items():
            if not await self._answers(service):
                await docker_ctl.docker("restart", "-t", "1", name)
                restarted.append(name)
        self._hung.clear()
        response = await self.ctx.http.post(f"{self.ctx.toxiproxy_url}/reset")
        response.raise_for_status()
        return {
            "started": started,
            "unpaused": unpaused,
            "restarted_hung": restarted,
            "toxics_reset": True,
        }

    async def _answers(self, service: str) -> bool:
        try:
            response = await self.ctx.http.get(f"http://{service}:8000/health/live", timeout=1)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

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
