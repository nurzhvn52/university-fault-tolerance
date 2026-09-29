"""Runs experiment batches from the host, with a fresh stack for every run.

    python -m lab.orchestrate --mode baseline --scenarios E1,E2 --reps 5 --tag final

Results go to results/<tag>/<mode>/<scenario>/rep<k>/. A run whose metrics.json already
exists is skipped, so an interrupted batch can be continued with the same command.
Needs only the Python standard library and Docker Compose.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = {"baseline": "docker-compose.baseline.yml", "ft": "docker-compose.ft.yml"}


def compose(mode: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["docker", "compose", "-f", COMPOSE_FILES[mode], *args]
    return subprocess.run(command, cwd=ROOT, check=check)


def git_commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    commit = git("rev-parse", "--short", "HEAD")
    dirty = git("status", "--porcelain", "--", ".", ":!results")
    return commit + ("-dirty" if dirty else "")


def run_once(mode: str, scenario: str, rep: int, tag: str, commit: str) -> Path:
    relative = Path(tag) / mode / scenario / f"rep{rep}"
    compose(mode, "down", "-v", "--remove-orphans")
    compose(mode, "up", "-d")
    try:
        compose(
            mode,
            "run",
            "--rm",
            "-e",
            f"UFT_GIT_COMMIT={commit}",
            "lab",
            "python",
            "-m",
            "lab.run",
            "--scenario",
            scenario,
            "--mode",
            mode,
            "--rep",
            str(rep),
            "--out",
            f"/results/{relative.as_posix()}",
        )
    finally:
        compose(mode, "down", "-v", "--remove-orphans", check=False)
    return ROOT / "results" / relative


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", required=True, choices=sorted(COMPOSE_FILES))
    parser.add_argument("--scenarios", required=True, help="comma separated, e.g. E1,E2")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--tag", default="dev")
    parser.add_argument("--force", action="store_true", help="repeat runs that already exist")
    args = parser.parse_args()

    commit = git_commit()
    compose(args.mode, "--profile", "lab", "build")
    for scenario in args.scenarios.split(","):
        for rep in range(1, args.reps + 1):
            target = ROOT / "results" / args.tag / args.mode / scenario / f"rep{rep}"
            if (target / "metrics.json").exists() and not args.force:
                print(f"skip {args.mode} {scenario} rep{rep}: already done", flush=True)
                continue
            started = time.monotonic()
            print(f"=== {args.mode} {scenario} rep{rep} ({commit})", flush=True)
            out = run_once(args.mode, scenario, rep, args.tag, commit)
            metrics_file = out / "metrics.json"
            if not metrics_file.exists():
                print(f"!!! {scenario} rep{rep} produced no metrics", file=sys.stderr, flush=True)
                continue
            metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
            fault = metrics["fault"] or {}
            print(
                f"--- availability={metrics['requests']['availability']} "
                f"failed={metrics['requests']['failed']} recovery_s={fault.get('recovery_s')} "
                f"violations={ {k: v for k, v in metrics['consistency'].items() if v} } "
                f"({time.monotonic() - started:.0f} s)",
                flush=True,
            )


if __name__ == "__main__":
    main()
