"""Live demonstrations of the endterm features, for presenting the project.

Run inside the lab container of the endterm stack:

    python -m lab.init_secrets
    UFT_TRACING=true UFT_SECURITY=true docker compose -f docker-compose.ft.yml \
        -f docker-compose.endterm.yml --profile monitoring up -d --build
    docker compose -f docker-compose.ft.yml -f docker-compose.endterm.yml \
        --profile monitoring run --rm lab python -m lab.demo_endterm security
    ... python -m lab.demo_endterm alert        # a service fails, the alert fires, follow the id
    ... python -m lab.demo_endterm login        # password guessing

(the failure demo of the midterm, ``python -m lab.demo crash``, works on this stack too.)
"""

import argparse
import asyncio
import os
import time

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings
from lab import attacks, docker_ctl, observe, security_check
from lab.actions import ActionRunner, LabContext
from lab.demo import DemoWorkload
from lab.loadgen import LoadGenerator, make_plan
from lab.run import load_population, wait_ready
from lab.tokens import Tokens


def print_probes(title: str, summary: dict) -> None:
    print(f"\n{title}")
    for probe in summary["probes"]:
        verdict = "refused" if probe["refused"] else "ALLOWED"
        leak = "  DATA LEAKED" if probe["leaked"] else ""
        print(f"  {probe['name']:<26} {probe['status']:>3}  {probe['reason']:<18} {verdict}{leak}")
    print(
        f"  refused {summary['refused']} of {summary['total']}, succeeded {summary['succeeded']}, "
        f"leaked {summary['leaked']}, server errors {summary['server_errors']}"
    )


async def security(ctx: LabContext, engine, gateway: str) -> None:
    before = await security_check.snapshot(engine, ctx.victims)
    probes = attacks.unauthorized_probes(ctx.tokens, ctx.attacker_id, ctx.victims[0])
    print_probes(
        "Unauthorized access (one student attacks):",
        await attacks.run_probes(gateway, probes, ctx.markers),
    )
    print_probes(
        "Malformed and oversized input:",
        await attacks.run_probes(
            gateway, attacks.malformed_probes(ctx.tokens, ctx.attacker_id), []
        ),
    )
    flood = await attacks.flood(
        gateway, ctx.tokens.attacker(ctx.attacker_id), ctx.attacker_id, 150, 10
    )
    print(f"\nFlood from one client, 150 requests/s for 10 s: {flood['statuses']}")
    after = await security_check.snapshot(engine, ctx.victims)
    changed = {k: v["violations"] for k, v in security_check.diff(before, after).items()}
    print("data of the victims changed:", {k: v for k, v in changed.items() if v} or "nothing")
    await asyncio.sleep(3)
    rows = await security_check.audit_rows(engine, time.time() - 120)
    denied = [r for r in rows if r["action"] in ("auth.denied", "rate_limit")]
    print(f"audit trail: {len(denied)} denial events in the last two minutes, for example:")
    for row in denied[:5]:
        print(
            f"  {row['action']} actor={row['actor']} request_id={row['request_id']} {row['detail']}"
        )


async def login(ctx: LabContext, gateway: str) -> None:
    async with httpx.AsyncClient(base_url=gateway, timeout=10) as client:
        for i in range(1, 9):
            response = await client.post(
                "/api/auth/login", json={"username": "250050", "password": "wrong-password"}
            )
            print(f"attempt {i}: {response.status_code} {response.json().get('detail')}")
        right = await client.post(
            "/api/auth/login", json={"username": "250050", "password": ctx.seed_password}
        )
        print(f"correct password right after: {right.status_code} (the account is locked)")
        other = await client.post(
            "/api/auth/login", json={"username": "registrar1", "password": ctx.seed_password}
        )
        print(f"another user logs in meanwhile: {other.status_code}")


async def alert(ctx: LabContext, gateway: str, project: str, prometheus: str, sink: str) -> None:
    async with (
        httpx.AsyncClient(base_url=gateway, timeout=30) as client,
        httpx.AsyncClient(timeout=10) as admin,
    ):
        client.headers["Authorization"] = f"Bearer {ctx.tokens.admin()}"
        population = await load_population(client)
        runner = ActionRunner(ctx)
        generator = LoadGenerator(client)
        workload = DemoWorkload(
            population, seed=time.time_ns() % 2**31, token_for=ctx.tokens.student
        )
        start = time.monotonic()
        load = asyncio.create_task(generator.run(make_plan([(60, 20)], workload), start))
        await asyncio.sleep(10)
        injected = time.time()
        print("*** FAULT: the payment-1 process hangs (alive, but answers nothing)", flush=True)
        await runner.execute({"do": "hang", "target": "payment-1", "seconds": 3600}, 10)
        seen: set[str] = set()
        while time.monotonic() < start + 55:
            await asyncio.sleep(1)
            for name in await observe.firing_now(admin, prometheus):
                if name not in seen:
                    seen.add(name)
                    print(f"  +{time.time() - injected:4.1f}s alert firing: {name}", flush=True)
        await load
        notes = (await admin.get(f"{sink}/received")).json()
        print("\nnotifications received by the alert sink:")
        for n in notes:
            if n["received_at"] >= injected:
                print(f"  +{n['received_at'] - injected:4.1f}s {n['status']:<9} {n['alertname']}")
        failed = [a for a in generator.attempts if not (0 < a.status < 500 and a.status != 429)]
        print(f"\nclient view: {len(failed)} failed requests of {len(generator.attempts)}")
        print("diagnosis: pick a failed request id from the service logs and follow it:")
        lines = []
        for container in await docker_ctl.containers(project):
            if container.labels.get("uft.role") == "app":
                lines += [
                    (t, container.service, x)
                    for t, x in await docker_ctl.logs(container.name, injected)
                ]
        ids = [
            x.split('"request_id": "')[1].split('"')[0]
            for _, _, x in lines
            if '"request_failed"' in x
        ]
        if ids:
            wanted = ids[0]
            print(f"request id {wanted}:")
            for _t, service, text in sorted(lines):
                if wanted in text:
                    print(f"  {service:<12} {text[:200]}")
        else:
            print("  (no request failed with a server error: the gateway retried on payment-2)")


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    gateway = os.environ["UFT_GATEWAY_URL"]
    project = os.environ["UFT_PROJECT"]
    tokens = Tokens(settings)
    engine = create_async_engine(settings.database_url, pool_size=2)
    async with httpx.AsyncClient(
        base_url=gateway, timeout=30, headers={"Authorization": f"Bearer {tokens.admin()}"}
    ) as client:
        await wait_ready(client)
    victims = await security_check.victim_ids(engine, 10)
    markers = await security_check.victim_markers(engine, victims)
    async with httpx.AsyncClient(timeout=10) as http:
        ctx = LabContext(
            project,
            http,
            gateway,
            os.environ["UFT_TOXIPROXY_URL"],
            1,
            tokens=tokens,
            victims=victims,
            markers=markers,
            seed_password=os.environ.get("UFT_SEED_PASSWORD", "synthetic-lab-password"),
        )
        if args.demo == "security":
            await security(ctx, engine, gateway)
        elif args.demo == "login":
            await login(ctx, gateway)
        else:
            await alert(
                ctx,
                gateway,
                project,
                os.environ["UFT_PROMETHEUS_URL"],
                os.environ["UFT_ALERT_SINK_URL"],
            )
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("demo", choices=["security", "login", "alert"])
    asyncio.run(main(parser.parse_args()))
