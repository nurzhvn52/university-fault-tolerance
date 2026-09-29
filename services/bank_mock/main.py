"""Simulated external payment provider ("the bank").

Charges are kept in the bank's own SQLite file, so failures of the university database do
not affect it. Like real payment APIs, a request with an idempotency key that was already
used returns the original charge; a request without a key always creates a new charge.

The charge is stored first and the response is sent after the configured latency, so a
client that gives up waiting does not know whether it was charged.
"""

import asyncio
from decimal import Decimal

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bank_mock.store import ChargeStore
from common.app import create_service_app
from common.config import Settings, get_settings

router = APIRouter(tags=["bank"])


class ChargeIn(BaseModel):
    account: str = Field(max_length=64)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reference: str = Field(max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ChargeOut(BaseModel):
    charge_id: str
    account: str
    amount: Decimal
    reference: str
    idempotency_key: str | None
    status: str
    created_at: str


def _store(request: Request) -> ChargeStore:
    return request.app.state.charges


@router.post("/charges", response_model=ChargeOut, status_code=201)
async def create_charge(body: ChargeIn, request: Request, response: Response) -> dict:
    charge, created = _store(request).create(
        body.account, body.amount, body.reference, body.idempotency_key
    )
    if not created:
        response.status_code = 200
    await asyncio.sleep(request.app.state.settings.bank_latency_ms / 1000)
    return charge


@router.get("/charges/{charge_id}", response_model=ChargeOut)
async def get_charge(charge_id: str, request: Request) -> dict:
    charge = _store(request).get(charge_id)
    if charge is None:
        raise HTTPException(404, "charge_not_found")
    return charge


@router.get("/charges", response_model=list[ChargeOut])
async def find_charges(
    request: Request, reference: str | None = None, idempotency_key: str | None = None
) -> list:
    return _store(request).find(reference=reference, idempotency_key=idempotency_key)


@router.get("/summary")
async def summary(request: Request) -> dict:
    return _store(request).summary()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    async def open_store(app: FastAPI) -> None:
        app.state.charges = ChargeStore(settings.bank_db_path)

    async def close_store(app: FastAPI) -> None:
        app.state.charges.close()

    return create_service_app(
        settings,
        title="Bank (simulated payment provider)",
        routers=[router],
        use_db=False,
        on_startup=open_store,
        on_shutdown=close_store,
    )


app = create_app()
