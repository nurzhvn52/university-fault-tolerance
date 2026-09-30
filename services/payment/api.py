"""Payment API: tuition status and tuition payments."""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from common.chaos import fault_point
from common.db import SessionDep
from payment import journal
from payment.bank import BankClient
from payment.models import Invoice, Payment

logger = logging.getLogger("payment.api")
router = APIRouter(prefix="/api", tags=["payment"])


class TuitionOut(BaseModel):
    student_id: int
    term: str
    amount_due: Decimal
    amount_paid: Decimal
    balance: Decimal
    paid: bool

    @classmethod
    def from_invoice(cls, invoice: Invoice) -> "TuitionOut":
        balance = invoice.amount_due - invoice.amount_paid
        return cls(
            student_id=invoice.student_id,
            term=invoice.term,
            amount_due=invoice.amount_due,
            amount_paid=invoice.amount_paid,
            balance=balance,
            paid=balance <= 0,
        )


class PaymentIn(BaseModel):
    student_id: int = Field(gt=0)
    term: str = Field(max_length=16)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    term: str
    amount: Decimal
    status: str
    bank_charge_id: str | None
    created_at: datetime


async def _get_invoice(session, student_id: int, term: str) -> Invoice:
    invoice = await session.scalar(
        select(Invoice).where(Invoice.student_id == student_id, Invoice.term == term)
    )
    if invoice is None:
        raise HTTPException(404, "invoice_not_found")
    return invoice


@router.get("/tuition", response_model=list[TuitionOut])
async def list_tuition(
    session: SessionDep, term: str, paid: bool | None = None, limit: int = 100
) -> list[TuitionOut]:
    query = select(Invoice).where(Invoice.term == term).order_by(Invoice.student_id)
    if paid is not None:
        is_paid = Invoice.amount_paid >= Invoice.amount_due
        query = query.where(is_paid if paid else ~is_paid)
    invoices = await session.scalars(query.limit(min(limit, 1000)))
    return [TuitionOut.from_invoice(invoice) for invoice in invoices]


@router.get("/tuition/{student_id}", response_model=TuitionOut)
async def tuition_status(student_id: int, term: str, session: SessionDep) -> TuitionOut:
    return TuitionOut.from_invoice(await _get_invoice(session, student_id, term))


@router.post("/payments", response_model=PaymentOut, status_code=201)
async def create_payment(
    body: PaymentIn,
    request: Request,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
):
    if request.app.state.settings.ft:
        # 201 captured now, 200 repeated request (same key), 202 accepted and still pending.
        status, payment = await journal.create_payment(
            request.app,
            session,
            student_id=body.student_id,
            term=body.term,
            amount=body.amount,
            key=idempotency_key,
        )
        return JSONResponse(
            status_code=status, content=PaymentOut.model_validate(payment).model_dump(mode="json")
        )

    # Baseline: the Idempotency-Key header is ignored.
    invoice = await _get_invoice(session, body.student_id, body.term)
    if body.amount > invoice.amount_due - invoice.amount_paid:
        raise HTTPException(409, "amount_exceeds_balance")

    settings = request.app.state.settings
    bank = BankClient(request.app.state.http, settings.bank_url)
    charge = await bank.charge(
        account=f"STU-{body.student_id}",
        amount=body.amount,
        reference=f"tuition:{body.student_id}:{body.term}",
    )

    # Baseline: the payment is recorded only after the bank has charged the student.
    # If the process dies here, the money is taken but there is no payment record.
    await fault_point("payment.after_charge")
    payment = Payment(
        student_id=body.student_id,
        term=body.term,
        amount=body.amount,
        status="CAPTURED",
        bank_charge_id=charge["charge_id"],
    )
    session.add(payment)
    await session.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id)
        .values(amount_paid=Invoice.amount_paid + body.amount)
    )
    await session.commit()
    logger.info(
        "payment_captured",
        extra={
            "payment_id": payment.id,
            "student_id": body.student_id,
            "amount": str(body.amount),
            "charge_id": charge["charge_id"],
        },
    )
    return payment


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(session: SessionDep, student_id: int) -> list[Payment]:
    payments = await session.scalars(
        select(Payment).where(Payment.student_id == student_id).order_by(Payment.id)
    )
    return list(payments)


@router.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: int, session: SessionDep) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(404, "payment_not_found")
    return payment
