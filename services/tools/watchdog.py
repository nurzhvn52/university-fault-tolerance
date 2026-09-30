"""Watchdog: restarts containers whose health check keeps failing.

Docker restarts a container whose process exits (restart policy), but not one whose process
hangs. A hung service fails its liveness check, and this loop restarts every running
container of the project labelled ``uft.watchdog=true`` that Docker reports unhealthy: the
software counterpart of a hardware watchdog timer (no heartbeat, so reset).

    python -m tools.watchdog --project uft-ft
"""

import argparse
import json
import logging
import subprocess
import time

from common.logs import configure_logging

logger = logging.getLogger("tools.watchdog")


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def unhealthy(project: str) -> list[str]:
    out = docker(
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        "label=uft.watchdog=true",
        "--filter",
        "health=unhealthy",
        "--format",
        "{{json .}}",
    )
    return [json.loads(line)["Names"] for line in out.splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    configure_logging("watchdog", "ops")
    logger.info("watchdog_started", extra={"project": args.project})
    while True:
        try:
            for name in unhealthy(args.project):
                logger.warning("watchdog_restart", extra={"container": name})
                docker("restart", "-t", "1", name)
        except subprocess.CalledProcessError as exc:
            logger.warning("watchdog_error", extra={"error": exc.stderr.strip()[:200]})
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
