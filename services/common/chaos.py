"""Fault points for failure-injection experiments.

Code marks places where a failure matters with ``await fault_point("payment.after_charge")``.
The experiment tooling arms a point through the admin endpoint (only mounted when
``UFT_CHAOS_ENABLED=true``, never routed by the gateway) and chooses what happens there:

- ``crash``: the process exits immediately, like a SIGKILL, without cleanup;
- ``error``: the code raises ``InjectedFault``;
- ``delay``: the code sleeps for ``delay_ms``.

``skip`` lets the first N hits pass, ``count`` limits how many hits trigger the action.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("common.chaos")

Action = Literal["crash", "error", "delay"]


class InjectedFault(Exception):
    pass


@dataclass
class ArmedFault:
    action: Action
    skip: int = 0
    count: int = 1
    delay_ms: int = 0


_faults: dict[str, ArmedFault] = {}


def arm(point: str, fault: ArmedFault) -> None:
    _faults[point] = fault


def disarm_all() -> None:
    _faults.clear()


async def fault_point(point: str) -> None:
    fault = _faults.get(point)
    if fault is None:
        return
    if fault.skip > 0:
        fault.skip -= 1
        return
    fault.count -= 1
    if fault.count <= 0:
        del _faults[point]
    logger.warning("fault_injected", extra={"point": point, "action": fault.action})
    if fault.action == "crash":
        for handler in logging.getLogger().handlers:
            handler.flush()
        os._exit(137)
    if fault.action == "error":
        raise InjectedFault(point)
    await asyncio.sleep(fault.delay_ms / 1000)


class FaultIn(BaseModel):
    point: str = Field(max_length=64)
    action: Action
    skip: int = Field(default=0, ge=0)
    count: int = Field(default=1, ge=1)
    delay_ms: int = Field(default=0, ge=0)


router = APIRouter(prefix="/_chaos", tags=["chaos"])


@router.post("/faults", status_code=201)
async def arm_fault(body: FaultIn) -> dict:
    arm(body.point, ArmedFault(body.action, body.skip, body.count, body.delay_ms))
    logger.info("fault_armed", extra=body.model_dump())
    return body.model_dump()


@router.delete("/faults", status_code=204)
async def clear_faults() -> None:
    disarm_all()


@router.get("/faults")
async def list_faults() -> dict:
    return {point: vars(fault) for point, fault in _faults.items()}


@router.post("/crash")
async def crash() -> None:
    """The process dies at once (a crash, not a stop), so the restart policy applies."""
    logger.warning("fault_injected", extra={"point": "process", "action": "crash"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    os._exit(137)


@router.post("/hang")
async def hang(seconds: float = 3600) -> None:
    """Blocks the event loop: the process is alive but answers nothing, not even health
    checks. Only a watchdog notices and fixes this."""
    logger.warning("fault_injected", extra={"point": "process", "action": "hang"})
    time.sleep(seconds)
