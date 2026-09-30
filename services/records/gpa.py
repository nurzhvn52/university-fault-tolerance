"""Grade points and GPA (4.0 scale, weighted by credits)."""

import math
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

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


# N-version programming (FT mode): three independently written versions of the same GPA
# specification and a majority voter. A fault in one version is masked by the other two.


def gpa_hundredths(grades: Iterable[tuple[Decimal, int]]) -> tuple[Decimal, int]:
    """Version 2: integer arithmetic in hundredths of a grade point."""
    weighted = credits = 0
    for points, course_credits in grades:
        weighted += int(points * 100) * course_credits
        credits += course_credits
    if credits == 0:
        return Decimal("0.00"), 0
    quotient, remainder = divmod(weighted, credits)
    if 2 * remainder >= credits:
        quotient += 1
    return Decimal(quotient) / 100, credits


def gpa_fraction(grades: Iterable[tuple[Decimal, int]]) -> tuple[Decimal, int]:
    """Version 3: exact rational arithmetic, rounded half up at the end."""
    grades = list(grades)
    credits = sum(c for _, c in grades)
    if credits == 0:
        return Decimal("0.00"), 0
    exact = sum((Fraction(p) * c for p, c in grades), Fraction(0)) / credits
    hundredths = math.floor(exact * 100 + Fraction(1, 2))
    return Decimal(hundredths) / 100, credits


VERSIONS = (compute_gpa, gpa_hundredths, gpa_fraction)


class NoMajority(Exception):
    pass


def voted_gpa(grades, versions=VERSIONS) -> tuple[tuple[Decimal, int], int]:
    """Majority vote over the versions. Returns (result, number of versions outvoted or
    failed); raises NoMajority when no two versions agree."""
    grades = list(grades)
    results = []
    for version in versions:
        try:
            gpa, credits = version(grades)
            results.append((gpa.quantize(Decimal("0.01")), credits))
        except Exception:
            results.append(None)
    for candidate in results:
        if candidate is not None and results.count(candidate) * 2 > len(results):
            return candidate, len(results) - results.count(candidate)
    raise NoMajority(results)
