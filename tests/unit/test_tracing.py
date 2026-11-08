import asyncio
import json
import logging

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from common.app import create_service_app, forward_request_id
from common.config import Settings
from common.context import current_request_id, request_id_var
from common.logs import JsonFormatter
from common.resilience import CircuitOpen, Dependency
from common.telemetry import DEPENDENCY_CALLS

router = APIRouter(prefix="/api")


@router.get("/whoami")
async def whoami() -> dict:
    logging.getLogger("test").info("handled")
    return {"request_id": current_request_id()}


def make_client(tracing: bool) -> TestClient:
    settings = Settings(service_name="tracetest", tracing=tracing)
    return TestClient(create_service_app(settings, title="t", routers=[router], use_db=False))


def test_request_id_kept_generated_and_replaced():
    with make_client(True) as client:
        kept = client.get("/api/whoami", headers={"X-Request-ID": "abc-12345678"})
        assert kept.headers["x-request-id"] == "abc-12345678"
        assert kept.json()["request_id"] == "abc-12345678"

        generated = client.get("/api/whoami")
        assert len(generated.headers["x-request-id"]) == 32

        # Log injection attempt: a malformed id must not reach the logs.
        bad = client.get("/api/whoami", headers={"X-Request-ID": 'x"}{"event":"forged'})
        assert bad.headers["x-request-id"] != 'x"}{"event":"forged'
        assert bad.json()["request_id"] == bad.headers["x-request-id"]


def test_midterm_configuration_adds_no_request_id():
    with make_client(False) as client:
        response = client.get("/api/whoami", headers={"X-Request-ID": "abc-12345678"})
        assert "x-request-id" not in response.headers
        assert response.json()["request_id"] is None


def test_log_lines_carry_request_id_and_outgoing_calls_forward_it():
    record = logging.makeLogRecord({"msg": "hello", "levelno": logging.INFO, "levelname": "INFO"})
    token = request_id_var.set("req-0123456789")
    try:
        line = json.loads(JsonFormatter("svc", "a").format(record))
        outgoing = httpx.Request("GET", "http://payment/api/tuition/1")
        asyncio.run(forward_request_id(outgoing))
    finally:
        request_id_var.reset(token)
    assert line["request_id"] == "req-0123456789"
    assert outgoing.headers["x-request-id"] == "req-0123456789"


def test_dependency_metrics_count_each_outcome():
    dependency = Dependency(
        "metrics-dep",
        timeout_s=0.05,
        attempts=1,
        base_delay_s=0,
        max_delay_s=0,
        breaker_failures=2,
        breaker_reset_s=60,
    )

    async def slow():
        await asyncio.sleep(1)

    async def ok():
        return 1

    def count(outcome: str) -> float:
        return DEPENDENCY_CALLS.labels("metrics-dep", outcome)._value.get()

    async def scenario():
        assert await dependency.call(ok) == 1
        for _ in range(2):
            with pytest.raises(Exception):  # noqa: B017
                await dependency.call(slow)
        with pytest.raises(CircuitOpen):
            await dependency.call(ok)

    asyncio.run(scenario())
    assert (count("ok"), count("timeout"), count("circuit_open")) == (1, 2, 1)
