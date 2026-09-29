import asyncio

import pytest

from common import chaos


@pytest.fixture(autouse=True)
def clean_faults():
    chaos.disarm_all()
    yield
    chaos.disarm_all()


def hit(point: str) -> None:
    asyncio.run(chaos.fault_point(point))


def test_unarmed_point_does_nothing():
    hit("payment.after_charge")


def test_error_fires_after_skipped_hits_and_only_count_times():
    chaos.arm("p", chaos.ArmedFault("error", skip=2, count=1))

    hit("p")
    hit("p")
    with pytest.raises(chaos.InjectedFault):
        hit("p")
    hit("p")


def test_crash_exits_the_process(monkeypatch):
    codes = []
    monkeypatch.setattr(chaos.os, "_exit", codes.append)
    chaos.arm("p", chaos.ArmedFault("crash"))

    hit("p")

    assert codes == [137]
