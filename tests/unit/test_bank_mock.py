import pytest
from fastapi.testclient import TestClient

from bank_mock.main import create_app
from common.config import Settings


@pytest.fixture
def bank(tmp_path):
    settings = Settings(
        service_name="bank", bank_db_path=str(tmp_path / "bank.db"), bank_latency_ms=0
    )
    with TestClient(create_app(settings)) as client:
        yield client


def charge(client, **overrides):
    body = {"account": "STU-1", "amount": "1000.00", "reference": "tuition:1:2026-FALL"}
    return client.post("/charges", json=body | overrides)


def test_charge_without_key_creates_a_new_charge_every_time(bank):
    first = charge(bank)
    second = charge(bank)

    assert first.status_code == second.status_code == 201
    assert first.json()["charge_id"] != second.json()["charge_id"]
    assert bank.get("/summary").json() == {"count": 2, "total_amount": "2000.00"}


def test_repeated_idempotency_key_returns_the_original_charge(bank):
    first = charge(bank, idempotency_key="pay-1")
    replay = charge(bank, idempotency_key="pay-1")

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["charge_id"] == first.json()["charge_id"]
    assert bank.get("/summary").json()["count"] == 1


def test_charges_can_be_found_by_key_and_reference(bank):
    created = charge(bank, idempotency_key="pay-2").json()

    assert bank.get("/charges", params={"idempotency_key": "pay-2"}).json() == [created]
    assert bank.get(f"/charges/{created['charge_id']}").json() == created
    assert bank.get("/charges", params={"reference": "tuition:1:2026-FALL"}).json() == [created]
    assert bank.get("/charges/ch_missing").status_code == 404


@pytest.mark.parametrize("amount", ["0", "-5.00", "1.001"])
def test_invalid_amount_is_rejected(bank, amount):
    assert charge(bank, amount=amount).status_code == 422


def test_responses_name_the_instance(bank):
    assert bank.get("/health/live").headers["X-Instance"]
