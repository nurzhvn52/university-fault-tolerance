"""Scenarios, workload and load schedule of the experiment tooling."""

import asyncio
import time

import httpx
import pytest

from lab.loadgen import LoadGenerator, is_success, make_plan, schedule
from lab.scenarios import load
from lab.workload import SERVICE_OF, Population, Workload

POPULATION = Population(
    students=10, paid=[1, 2, 3], unpaid=[4, 5], with_grades=[1, 2], sections=[1, 2, 3, 4]
)


def test_all_scenarios_load_and_cover_the_required_failures():
    scenarios = load()

    assert {"E1", "E1b", "E2", "E3", "E4", "E5a", "E5b", "E5c", "E6", "E9", "CAL"} <= set(scenarios)
    assert scenarios["E1"].duration == 120
    assert scenarios["E1"].fault_at == 20
    assert scenarios["E6"].fault_at == 20
    assert scenarios["E5b"].expected_batch == 200
    assert scenarios["CAL"].fault_at is None


def test_schedule_follows_the_rate_profile():
    offsets = schedule([(2, 2), (1, 4)])

    assert offsets == [0, 0.5, 1.0, 1.5, 2.0, 2.25, 2.5, 2.75]


def test_business_rejections_count_as_answers():
    assert is_success(201) and is_success(409) and is_success(402)
    assert not is_success(500) and not is_success(503)
    assert not is_success(429) and not is_success(0)


def test_every_payment_of_a_student_has_a_different_amount():
    workload = Workload(POPULATION, seed=1, mix={"payment.create": 1})

    requests = [workload.next() for _ in range(50)]
    keys = [(r.json["student_id"], r.json["amount"]) for r in requests]

    assert len(set(keys)) == len(keys)
    assert all(r.headers["Idempotency-Key"] == r.intent for r in requests)
    assert all(r.retries == 2 for r in requests)


def test_workload_is_reproducible_and_covers_all_operations():
    first = [Workload(POPULATION, seed=7).next() for _ in range(1)]
    again = [Workload(POPULATION, seed=7).next() for _ in range(1)]
    ops = {Workload(POPULATION, seed=s).next().op for s in range(300)}

    assert first == again
    assert ops == set(SERVICE_OF)


def test_unknown_operation_is_rejected():
    with pytest.raises(ValueError):
        Workload(POPULATION, seed=1, mix={"student.delete": 1})


def test_failed_payment_is_retried_with_the_same_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        return httpx.Response(500 if len(seen) == 1 else 201)

    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            workload = Workload(POPULATION, seed=1, mix={"payment.create": 1})
            generator = LoadGenerator(client, retry_delay_s=0)
            await generator.run(make_plan([(1, 1)], workload), start_mono=time.monotonic())
            return generator.attempts

    attempts = asyncio.run(scenario())

    assert [a.status for a in attempts] == [500, 201]
    assert [a.attempt for a in attempts] == [1, 2]
    assert len(set(seen)) == 1


def test_long_run_failures_are_reproducible_and_repaired_one_at_a_time():
    first = load(seed=3)["E7"]
    again = load(seed=3)["E7"]
    other = load(seed=4)["E7"]

    assert first.actions == again.actions
    assert first.actions != other.actions
    kinds = [a["do"] for a in first.actions]
    assert kinds[0] != "repair" and kinds[-1] == "repair"
    assert all(kinds[i] != "repair" for i in range(0, len(kinds), 2))
    assert all(kinds[i] == "repair" for i in range(1, len(kinds), 2))
    assert all(not isinstance(v, list) for a in first.actions for v in a.values())
    assert first.actions[-1]["at"] <= first.duration - 30
