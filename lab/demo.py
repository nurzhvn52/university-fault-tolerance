"""Live demonstration of one failure and the recovery, for presenting the project.

Run it inside the lab container of a running stack, for example:

    docker compose -f docker-compose.ft.yml up -d --build
    docker compose -f docker-compose.ft.yml run --rm lab python -m lab.demo db
    docker compose -f docker-compose.baseline.yml run --rm lab python -m lab.demo db   # contrast

Every second it prints the share of requests that succeeded in that second, which replicas
answered and which database node is the primary; at the end it checks data consistency.
"""

import argparse
import asyncio
import contextlib
import os
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings
from lab import consistency
from lab.actions import ActionRunner, LabContext
from lab.loadgen import LoadGenerator, make_plan
from lab.metrics import good
from lab.run import load_population, wait_ready
from lab.workload import Workload

# failure -> (what happens, lab action, service whose replicas are shown every second)
FAILURES = {
    "crash": ("the payment-1 process crashes", {"do": "crash", "target": "payment-1"}, "payment"),
    "hang": (
        "the payment-1 process hangs",
        {"do": "hang", "target": "payment-1", "seconds": 3600},
        "payment",
    ),
    "db": ("the primary database is killed", {"do": "kill", "target": "db-primary"}, None),
    "node": (
        "node a is lost (one replica of everything and pg-1)",
        {"do": "kill_node", "node": "a"},
        "student",
    ),
    "network": (
        "calls from student to payment stop getting answers",
        {"do": "toxic", "proxy": "payment", "toxic": "timeout", "attributes": {"timeout": 0}},
        None,
    ),
    "payment": (
        "payment-1 crashes after the bank charged a student, before the payment is saved",
        {
            "do": "arm_fault",
            "target": "payment-1",
            "point": "payment.after_charge",
            "action": "crash",
        },
        "payment",
    ),
}


class DemoWorkload(Workload):
    """A fresh seed and amount range for every demo. Repeated demos on one stack would
    otherwise resend the idempotency keys of earlier runs (answered as duplicates, so nothing
    is charged) and reuse their amounts (which the double-charge check would flag)."""

    def __init__(self, population, seed: int):
        super().__init__(population, seed=seed)
        self.offset = Decimal(seed % 1000)

    def payment_amount(self, student_id: int) -> Decimal:
        return super().payment_amount(student_id) + self.offset


async def node_role(http: httpx.AsyncClient, node: str) -> str:
    try:
        data = (await http.get(f"http://{node}:8008/", timeout=0.3)).json()
        return f"{node}={data.get('role', '?')}"
    except (httpx.HTTPError, ValueError):
        return f"{node}=down"


async def primary_db(http: httpx.AsyncClient) -> str:
    # Both nodes at once with a short timeout, so a dead node does not slow the display.
    return " ".join(await asyncio.gather(*(node_role(http, n) for n in ("pg-1", "pg-2"))))


async def show(
    generator: LoadGenerator,
    http: httpx.AsyncClient,
    start: float,
    until: float,
    ft: bool,
    focus: str | None,
):
    seen = 0
    tick = start
    while time.monotonic() < until:
        tick += 1  # fixed one-second ticks, whatever the queries below take
        await asyncio.sleep(max(tick - time.monotonic(), 0))
        done = generator.attempts[seen:]  # attempts are recorded when they finish
        seen += len(done)
        ok = sum(good(a) for a in done)
        share = f"{ok / len(done) * 100:5.1f}%" if done else "  n/a "
        replicas = Counter(
            a.instance for a in done if a.instance and focus and a.instance.startswith(focus)
        )
        served = f"{focus} served by {dict(sorted(replicas.items()))}" if focus else ""
        db = await primary_db(http) if ft else ""
        print(
            f"t={time.monotonic() - start:5.1f}s  good {share} ({ok}/{len(done)})  {served}  {db}",
            flush=True,
        )


async def main(args: argparse.Namespace) -> None:
    gateway = os.environ["UFT_GATEWAY_URL"]
    project = os.environ["UFT_PROJECT"]
    ft = project.endswith("-ft")
    description, action, focus = FAILURES[args.failure]
    async with (
        httpx.AsyncClient(base_url=gateway, timeout=30) as client,
        httpx.AsyncClient(timeout=10) as admin,
    ):
        await wait_ready(client)
        population = await load_population(client)
        runner = ActionRunner(
            LabContext(project, admin, gateway, os.environ["UFT_TOXIPROXY_URL"], 1)
        )
        generator = LoadGenerator(client)
        plan = make_plan(
            [(args.duration, args.rate)], DemoWorkload(population, seed=time.time_ns() % 2**31)
        )
        start = time.monotonic()
        print(f"stack {project}: {args.rate} requests/s for {args.duration} s")
        print(f"failure at {args.at} s: {description}")
        load = asyncio.create_task(generator.run(plan, start))
        display = asyncio.create_task(
            show(generator, admin, start, start + args.duration, ft, focus)
        )

        await asyncio.sleep(args.at)
        print(f"*** FAILURE: {description}", flush=True)
        await runner.execute(action, args.at)
        if args.failure == "payment":
            print("    (armed: the next payment that reaches payment-1 crashes it)", flush=True)
        if args.repair:
            await asyncio.sleep(max(args.repair - args.at, 0))
            print(
                "*** REPAIR: an operator restarts what is down and removes network faults",
                flush=True,
            )
            await runner.execute({"do": "repair"}, args.repair)

        await display
        with contextlib.suppress(asyncio.CancelledError):
            await load

    attempts = generator.attempts
    ok = sum(good(a) for a in attempts)
    failed = len(attempts) - ok
    print(f"\navailability {ok / len(attempts) * 100:.2f}% ({failed} failed of {len(attempts)})")
    await asyncio.sleep(3)
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=1)
    roots = os.environ.get("UFT_TRANSCRIPT_DIRS", "/data/transcripts").split(",")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            report = await consistency.run_checks(
                engine, http, settings.bank_url, [Path(p) for p in roots if p], None
            )
    finally:
        await engine.dispose()
    problems = {k: v for k, v in consistency.violations(report).items() if v}
    pay = report["payments"]
    print(f"bank charges {pay['bank_charges']}, payment records {pay['payment_records']}")
    print("data consistency:", problems or "no violations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("failure", choices=sorted(FAILURES))
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--at", type=float, default=10, help="seconds before the failure")
    parser.add_argument(
        "--repair", type=float, default=0, help="seconds from start to a manual repair (0: none)"
    )
    asyncio.run(main(parser.parse_args()))
