"""One experiment run, executed inside the lab container on the stack network.

    python -m lab.run --scenario E1 --mode baseline --rep 1 --out /results/dev/baseline/E1/rep1

Steps: wait for the stack, start the open-loop load, execute the scenario actions on
schedule, let the system settle, check data consistency, collect Docker events and logs,
compute the metrics. Everything is written to the output directory.
"""

import argparse
import asyncio
import gzip
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings
from common.logs import configure_logging
from lab import consistency, docker_ctl, metrics
from lab.actions import ActionRunner, LabContext
from lab.loadgen import DistributedLoad, make_plan, write_attempts
from lab.scenarios import Scenario, load
from lab.workload import TERM, Population, Workload

logger = logging.getLogger("lab.run")

SETTLE_S = 5
READY_TIMEOUT_S = 180
READY_PATHS = [
    "/api/courses",
    f"/api/tuition/1?term={TERM}",
    "/api/transcripts/1",
    f"/api/timetable/sections?term={TERM}&ids=1",
]


async def wait_ready(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            statuses = [(await client.get(path)).status_code for path in READY_PATHS]
            if all(status == 200 for status in statuses):
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("the stack did not become ready")
        await asyncio.sleep(1)


async def load_population(client: httpx.AsyncClient) -> Population:
    async def tuition(paid: bool) -> list[int]:
        response = await client.get(
            "/api/tuition", params={"term": TERM, "paid": paid, "limit": 1000}
        )
        response.raise_for_status()
        return [invoice["student_id"] for invoice in response.json()]

    paid, unpaid = await tuition(True), await tuition(False)
    sections = [s["id"] for s in (await client.get("/api/sections", params={"term": TERM})).json()]
    students = len(paid) + len(unpaid)
    limit = asyncio.Semaphore(50)

    async def year(student_id: int) -> int:
        async with limit:
            return (await client.get(f"/api/students/{student_id}")).json()["year"]

    years = await asyncio.gather(*(year(i) for i in range(1, students + 1)))
    with_grades = [i for i, y in enumerate(years, 1) if y > 1]
    return Population(students, paid, unpaid, with_grades, sections)


async def run_actions(runner: ActionRunner, scenario: Scenario, start_mono: float) -> None:
    async def at(action: dict) -> None:
        await asyncio.sleep(max(start_mono + action["at"] - time.monotonic(), 0))
        await runner.execute(action, action["at"])

    await asyncio.gather(*(at(action) for action in scenario.actions))


async def check_consistency(http: httpx.AsyncClient, scenario: Scenario) -> dict:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=2)
    roots = [Path(p) for p in os.environ.get("UFT_TRANSCRIPT_DIRS", "/data/transcripts").split(",")]
    try:
        for attempt in range(1, 6):
            try:
                return await consistency.run_checks(
                    engine, http, settings.bank_url, roots, scenario.expected_batch
                )
            except Exception as exc:
                logger.warning("consistency_retry", extra={"attempt": attempt, "error": repr(exc)})
                await asyncio.sleep(3)
        return {"error": "consistency checks failed after 5 attempts"}
    finally:
        await engine.dispose()


async def collect_logs(project: str, since: float) -> list[dict]:
    lines = []
    for container in await docker_ctl.containers(project):
        for t, text in await docker_ctl.logs(container.name, since):
            lines.append({"t": t, "container": container.service, "text": text})
    return sorted(lines, key=lambda line: line["t"])


def write_jsonl_gz(rows: list[dict], path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, default=str) + "\n")


def write_json(data, path: Path) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


async def main(args: argparse.Namespace) -> None:
    configure_logging("lab", "client", "INFO")
    scenario = load(seed=args.rep)[args.scenario]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    project = os.environ["UFT_PROJECT"]
    gateway = os.environ["UFT_GATEWAY_URL"]
    workers = int(os.environ.get("UFT_LOAD_WORKERS", "4"))

    async with (
        httpx.AsyncClient(base_url=gateway, timeout=30) as client,
        httpx.AsyncClient(timeout=10) as admin,
    ):
        await wait_ready(client)
        population = await load_population(client)
        ctx = LabContext(project, admin, gateway, os.environ["UFT_TOXIPROXY_URL"], args.rep)
        runner = ActionRunner(ctx)
        workload = (
            Workload(population, seed=args.rep, mix=scenario.mix)
            if scenario.mix
            else Workload(population, seed=args.rep)
        )
        plan = make_plan(scenario.rate_profile, workload)
        load_pool = DistributedLoad(gateway, workers=workers)
        await load_pool.start()

        stream = docker_ctl.EventStream(project)
        await stream.start()
        logger.info("run_started", extra={"scenario": scenario.name, "mode": args.mode})
        t0, start_mono = time.time(), time.monotonic()
        attempts, _ = await asyncio.gather(
            load_pool.run(plan, start_mono), run_actions(runner, scenario, start_mono)
        )
        t_load_end = time.time()
        load_pool.close()
        await asyncio.sleep(SETTLE_S)
        report = await check_consistency(admin, scenario)
        events = await stream.stop()
        logs = metrics.parse_logs(await collect_logs(project, t0 - 1))

    fault_at = t0 + scenario.fault_at if scenario.fault_at is not None else None
    result = metrics.compute(
        attempts,
        t0,
        t0 + scenario.duration,
        fault_at,
        logs,
        events,
        runner.records,
        scenario.rate_profile,
    )
    result["consistency"] = consistency.violations(report)

    write_json(
        {
            "scenario": scenario.name,
            "title": scenario.title,
            "mode": args.mode,
            "rep": args.rep,
            "seed": args.rep,
            "git_commit": os.environ.get("UFT_GIT_COMMIT", "unknown"),
            "started_at": datetime.fromtimestamp(t0, UTC).isoformat(timespec="milliseconds"),
            "t0": t0,
            "load_finished_s": round(t_load_end - t0, 2),
            "planned_requests": len(plan),
            "load_workers": workers,
            "rate_profile": scenario.rate_profile,
            "population": {
                "students": population.students,
                "paid": len(population.paid),
                "unpaid": len(population.unpaid),
                "with_grades": len(population.with_grades),
                "sections": len(population.sections),
            },
        },
        out / "meta.json",
    )
    write_attempts(attempts, out / "requests.csv.gz")
    write_json(runner.records, out / "actions.json")
    write_json(report, out / "consistency.json")
    write_json(result, out / "metrics.json")
    write_jsonl_gz(events, out / "events.jsonl.gz")
    write_jsonl_gz(
        [{k: v for k, v in e.items() if k != "json"} for e in logs], out / "logs.jsonl.gz"
    )

    fault = result["fault"] or {}
    logger.info(
        "run_finished",
        extra={
            "scenario": scenario.name,
            "mode": args.mode,
            "rep": args.rep,
            "availability": result["requests"]["availability"],
            "failed": result["requests"]["failed"],
            "recovery_s": fault.get("recovery_s"),
            "violations": {k: v for k, v in result["consistency"].items() if v},
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--mode", required=True, choices=["baseline", "sw", "ft"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--out", required=True)
    asyncio.run(main(parser.parse_args()))
