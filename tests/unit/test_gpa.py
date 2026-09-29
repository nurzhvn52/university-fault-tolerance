from decimal import Decimal

from records.gpa import GRADE_POINTS, compute_gpa


def test_gpa_is_weighted_by_credits():
    gpa, credits = compute_gpa([(GRADE_POINTS["A"], 6), (GRADE_POINTS["C"], 3)])

    # (4.00 * 6 + 2.00 * 3) / 9 = 3.333...
    assert gpa == Decimal("3.33")
    assert credits == 9


def test_gpa_rounds_half_up():
    gpa, _ = compute_gpa([(Decimal("3.67"), 1), (Decimal("3.00"), 1)])

    assert gpa == Decimal("3.34")


def test_no_grades_gives_zero():
    assert compute_gpa([]) == (Decimal("0.00"), 0)
