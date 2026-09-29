"""Async wrapper around the docker CLI; the lab container talks to the host daemon."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime


class DockerError(Exception):
    pass


@dataclass(frozen=True)
class Container:
    name: str
    service: str  # compose service name, e.g. "payment-1"
    state: str
    labels: dict[str, str]

    @property
    def running(self) -> bool:
        return self.state == "running"


async def docker(*args: str, check: bool = True) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    if check and process.returncode != 0:
        raise DockerError(f"docker {' '.join(args)}: {err.decode().strip()}")
    return out.decode(), err.decode()


async def containers(project: str) -> list[Container]:
    out, _ = await docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}")
    ids = out.split()
    if not ids:
        return []
    out, _ = await docker("inspect", *ids)
    result = []
    for item in json.loads(out):
        labels = item["Config"]["Labels"] or {}
        result.append(
            Container(
                name=item["Name"].lstrip("/"),
                service=labels.get("com.docker.compose.service", ""),
                state=item["State"]["Status"],
                labels=labels,
            )
        )
    return result


async def kill(name: str) -> None:
    await docker("kill", name)


async def start(name: str) -> None:
    await docker("start", name)


async def stop(name: str, timeout_s: int = 10) -> None:
    await docker("stop", "-t", str(timeout_s), name)


async def pause(name: str) -> None:
    await docker("pause", name)


async def unpause(name: str) -> None:
    await docker("unpause", name)


EVENT_TYPES = (
    "start",
    "die",
    "kill",
    "stop",
    "pause",
    "unpause",
    "restart",
    "oom",
    "health_status",
)


class EventStream:
    """Container events read live for the whole run.

    Asking the daemon afterwards with --since is not reliable: it keeps only the last 256
    events, and health-check exec events push the interesting ones out.
    """

    def __init__(self, project: str) -> None:
        self._project = project
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self.events: list[dict] = []

    async def start(self) -> None:
        filters = [f"label=com.docker.compose.project={self._project}"]
        filters += [f"event={name}" for name in EVENT_TYPES]
        args = [part for f in filters for part in ("--filter", f)]
        self._process = await asyncio.create_subprocess_exec(
            "docker", "events", *args, "--format", "{{json .}}", stdout=asyncio.subprocess.PIPE
        )
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for line in self._process.stdout:
            if line.strip():
                self.events.append(json.loads(line))

    async def stop(self) -> list[dict]:
        await asyncio.sleep(1)  # let the last events arrive
        self._process.terminate()
        await self._process.wait()
        await self._reader
        return self.events


def parse_docker_time(stamp: str) -> float:
    """RFC 3339 with nanoseconds (from ``docker logs --timestamps``) to epoch seconds."""
    date, _, fraction = stamp.rstrip("Z").partition(".")
    seconds = datetime.fromisoformat(date + "+00:00").timestamp()
    return seconds + float(f"0.{fraction or 0}")


async def logs(name: str, since: float) -> list[tuple[float, str]]:
    """Log lines of a container (stdout and stderr) with their Docker timestamps."""
    out, err = await docker("logs", "--timestamps", "--since", f"{since:.3f}", name, check=False)
    lines = []
    for raw in (out + err).splitlines():
        stamp, _, text = raw.partition(" ")
        try:
            lines.append((parse_docker_time(stamp), text))
        except ValueError:
            continue
    return sorted(lines)
