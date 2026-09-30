"""Fault-tolerant payment processing (FT mode): journal first, idempotent charge, roll forward.

1. The request is saved as a PENDING payment with its Idempotency-Key before the bank is
   called. This journal entry is the checkpoint the payment can be recovered from.
2. The bank is called with the same key, so a repeated call returns the first charge
   instead of charging again.
3. The charge id and the invoice update are saved in one transaction. The update is
   conditional (``WHERE status = 'PENDING'``), so two workers finishing the same payment
   cannot add it to the invoice twice.

If the process dies between 2 and 3, or the bank cannot be reached, the payment stays
PENDING. A repeated request with the same key, or the recovery worker, completes it by
repeating step 2 (roll-forward recovery). No database connection is held while the bank is
called. Fail-safe rule: an unknown outcome is never retried without the key.
"""

import asyncio
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from common.chaos import fault_point
from common.resilience import Dependency, DependencyUnavailable, RateLimitedLog
from payment.bank import BankClient
from payment.models import Invoice, Payment

logger = logging.getLogger("payment.journal")
_rate_log = RateLimitedLog()


def bank_dependency(settings) -> Dependency:
    return Dependency.from_settings("bank", settings, settings.bank_timeout_s)


async def capture(
    app: FastAPI,
    session: AsyncSession,
    *,
    payment_id: int,
    student_id: int,
    term: str,
    amount: Decimal,
    key: str,
) -> bool:
    """Charge the bank for a PENDING payment and save the result. Returns True if this call
    captured it, False if it was already captured. Raises DependencyUnavailable."""
    client = BankClient(app.state.http, app.state.settings.bank_url)
    charge = await app.state.bank.call(
        lambda: client.charge(
            account=f"STU-{student_id}",
            amount=amount,
            reference=f"tuition:{student_id}:{term}",
            idempotency_key=key,
        )
    )
    await fault_point("payment.after_charge")
    captured = await session.execute(
        update(Payment)
        .where(Payment.id == payment_id, Payment.status == "PENDING")
        .values(status="CAPTURED", bank_charge_id=charge["charge_id"], updated_at=func.now())
        .returning(Payment.id)
        .execution_options(synchronize_session=False)
    )
    done = captured.first() is not None
    if done:
        await session.execute(
            update(Invoice)
            .where(Invoice.student_id == student_id, Invoice.term == term)
            .values(amount_paid=Invoice.amount_paid + amount)
        )
    await session.commit()
    if done:
        logger.info(
            "payment_captured",
            extra={
                "payment_id": payment_id,
                "amount": str(amount),
                "charge_id": charge["charge_id"],
            },
        )
    return done


async def _finish(app: FastAPI, session: AsyncSession, payment: Payment) -> tuple[int, Payment]:
    """Try to capture a PENDING payment now; 201 if captured, 202 if it stays pending."""
    try:
        await capture(
            app,
            session,
            payment_id=payment.id,
            student_id=payment.student_id,
            term=payment.term,
            amount=payment.amount,
            key=payment.idempotency_key,
        )
    except DependencyUnavailable:
        if _rate_log.should_log("deferred"):
            logger.warning("payment_deferred", extra={"payment_id": payment.id})
        await session.rollback()
        return 202, payment
    await session.refresh(payment)
    return (201 if payment.status == "CAPTURED" else 202), payment


async def _replay(app: FastAPI, session: AsyncSession, payment: Payment) -> tuple[int, Payment]:
    """A request with a key that was already used: answer with the original payment."""
    logger.info("duplicate_request", extra={"payment_id": payment.id, "status": payment.status})
    if payment.status == "CAPTURED":
        return 200, payment
    if payment.status == "FAILED":
        raise HTTPException(409, "payment_failed")
    grace = app.state.settings.pending_grace_s
    age = await session.scalar(
        select(func.extract("epoch", func.now() - Payment.updated_at)).where(
            Payment.id == payment.id
        )
    )
    await session.commit()
    if age is not None and age > grace:
        return await _finish(app, session, payment)
    return 202, payment


async def create_payment(
    app: FastAPI,
    session: AsyncSession,
    *,
    student_id: int,
    term: str,
    amount: Decimal,
    key: str | None,
) -> tuple[int, Payment]:
    key = key or f"srv-{uuid.uuid4().hex}"
    existing = await session.scalar(select(Payment).where(Payment.idempotency_key == key))
    if existing is not None:
        return await _replay(app, session, existing)

    invoice = await session.scalar(
        select(Invoice).where(Invoice.student_id == student_id, Invoice.term == term)
    )
    if invoice is None:
        raise HTTPException(404, "invoice_not_found")
    if amount > invoice.amount_due - invoice.amount_paid:
        raise HTTPException(409, "amount_exceeds_balance")

    payment = Payment(
        student_id=student_id, term=term, amount=amount, status="PENDING", idempotency_key=key
    )
    session.add(payment)
    try:
        await session.commit()
    except IntegrityError:
        # The same key arrived twice at the same time; the other request owns the payment.
        await session.rollback()
        existing = await session.scalar(select(Payment).where(Payment.idempotency_key == key))
        if existing is None:
            raise HTTPException(409, "payment_in_progress") from None
        return await _replay(app, session, existing)
    return await _finish(app, session, payment)


async def reconcile_pending(app: FastAPI) -> int:
    """Complete PENDING payments nobody is working on. Rows are claimed by moving their
    updated_at forward (SKIP LOCKED keeps two replicas from claiming the same rows)."""
    settings = app.state.settings
    sessionmaker = app.state.sessionmaker
    grace = timedelta(seconds=settings.pending_grace_s)
    async with sessionmaker() as session:
        stale = (
            select(Payment.id)
            .where(Payment.status == "PENDING", Payment.updated_at < func.now() - grace)
            .order_by(Payment.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
        claimed = (
            await session.execute(
                update(Payment)
                .where(Payment.id.in_(stale.scalar_subquery()))
                .values(updated_at=func.now())
                .returning(
                    Payment.id,
                    Payment.student_id,
                    Payment.term,
                    Payment.amount,
                    Payment.idempotency_key,
                )
                .execution_options(synchronize_session=False)
            )
        ).all()
        await session.commit()
    completed = 0
    for row in claimed:
        async with sessionmaker() as session:
            try:
                done = await capture(
                    app,
                    session,
                    payment_id=row.id,
                    student_id=row.student_id,
                    term=row.term,
                    amount=row.amount,
                    key=row.idempotency_key,
                )
            except DependencyUnavailable:
                break
        if done:
            completed += 1
            logger.info("payment_reconciled", extra={"payment_id": row.id})
    return completed


async def recovery_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(app.state.settings.worker_interval_s)
        try:
            await reconcile_pending(app)
        except Exception as exc:
            if _rate_log.should_log("recovery"):
                logger.warning("payment_recovery_failed", extra={"error": type(exc).__name__})
