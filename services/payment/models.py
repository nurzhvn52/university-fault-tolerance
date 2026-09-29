"""Payment service tables (schema "payment")."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.db import schema_metadata

Money = Numeric(12, 2)


class Base(DeclarativeBase):
    metadata = schema_metadata("payment")


class Invoice(Base):
    """Tuition invoice of one student for one term.

    ``opening_paid`` is the amount paid before the system started (seed data), so
    ``amount_paid == opening_paid + sum of captured payments`` must always hold.
    """

    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("student_id", "term"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int]
    term: Mapped[str] = mapped_column(String(16))
    amount_due: Mapped[Decimal] = mapped_column(Money)
    opening_paid: Mapped[Decimal] = mapped_column(Money, default=0, server_default="0")
    amount_paid: Mapped[Decimal] = mapped_column(Money, default=0, server_default="0")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(index=True)
    term: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(16))
    bank_charge_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
