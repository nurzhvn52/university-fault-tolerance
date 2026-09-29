"""Grade points and GPA (4.0 scale, weighted by credits)."""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

GRADE_POINTS = {
    "A": Decimal("4.00"),
    "A-": Decimal("3.67"),
    "B+": Decimal("3.33"),
    "B": Decimal("3.00"),
    "B-": Decimal("2.67"),
    "C+": Decimal("2.33"),
    "C": Decimal("2.00"),
    "C-": Decimal("1.67"),
    "D+": Decimal("1.33"),
    "D": Decimal("1.00"),
    "F": Decimal("0.00"),
}


def compute_gpa(grades: Iterable[tuple[Decimal, int]]) -> tuple[Decimal, int]:
    """Return (GPA rounded to 2 places, total credits) for (points, credits) pairs."""
    weighted = Decimal(0)
    credits = 0
    for points, course_credits in grades:
        weighted += points * course_credits
        credits += course_credits
    if credits == 0:
        return Decimal("0.00"), 0
    return (weighted / credits).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), credits
