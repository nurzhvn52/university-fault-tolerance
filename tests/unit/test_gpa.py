import random
from decimal import Decimal

import pytest

from records.gpa import (
    GRADE_POINTS,
    VERSIONS,
    NoMajority,
    compute_gpa,
    gpa_fraction,
    gpa_hundredths,
    voted_gpa,
)


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


def test_the_three_versions_agree_on_random_transcripts():
    rng = random.Random(3)
    letters = list(GRADE_POINTS)
    for _ in range(500):
        grades = [(GRADE_POINTS[rng.choice(letters)], rng.randint(1, 8)) for _ in range(12)]
        results = {version(grades) for version in VERSIONS}
        assert len(results) == 1, (grades, results)


def test_voter_masks_one_faulty_version():
    grades = [(GRADE_POINTS["A"], 6), (GRADE_POINTS["C"], 3)]

    def broken(_):
        return Decimal("9.99"), 9

    def crashing(_):
        raise ZeroDivisionError

    assert voted_gpa(grades, (compute_gpa, broken, gpa_fraction)) == ((Decimal("3.33"), 9), 1)
    assert voted_gpa(grades, (crashing, gpa_hundredths, gpa_fraction)) == ((Decimal("3.33"), 9), 1)
    assert voted_gpa(grades) == ((Decimal("3.33"), 9), 0)


def test_voter_fails_without_a_majority():
    def broken(_):
        return Decimal("1.00"), 1

    with pytest.raises(NoMajority):
        voted_gpa([(GRADE_POINTS["A"], 1)], (compute_gpa, broken, lambda _: 1 / 0))
