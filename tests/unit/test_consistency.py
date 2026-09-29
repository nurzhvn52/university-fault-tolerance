import hashlib
from decimal import Decimal

from lab.consistency import (
    check_documents,
    check_grade_batch,
    check_payments,
    check_sections,
    violations,
)

TERM = "2026-FALL"


def payment(pid, student, amount, charge, status="CAPTURED"):
    return {
        "id": pid,
        "student_id": student,
        "term": TERM,
        "amount": Decimal(amount),
        "status": status,
        "bank_charge_id": charge,
    }


def charge(cid, student, amount):
    return {"charge_id": cid, "account": f"STU-{student}", "amount": amount}


def invoice(student, paid, opening="0"):
    return {
        "student_id": student,
        "term": TERM,
        "opening_paid": Decimal(opening),
        "amount_paid": Decimal(paid),
    }


def test_consistent_payments_have_no_violations():
    report = check_payments(
        [payment(1, 7, "100.01", "ch1")],
        [charge("ch1", 7, "100.01")],
        [invoice(7, "600.01", opening="500")],
    )

    assert violations({"payments": report}) == {
        "lost_payments": 0,
        "duplicate_charges": 0,
        "phantom_payments": 0,
        "invoice_mismatch": 0,
        "unfinished_payments": 0,
    }


def test_charge_without_payment_record_is_a_lost_payment():
    report = check_payments([], [charge("ch1", 7, "100.01")], [invoice(7, "0")])

    assert report["lost_payments"] == {"violations": 1, "amount": "100.01"}


def test_two_charges_of_one_payment_are_a_duplicate():
    report = check_payments(
        [payment(1, 7, "100.01", "ch1"), payment(2, 7, "100.01", "ch2")],
        [charge("ch1", 7, "100.01"), charge("ch2", 7, "100.01"), charge("ch3", 7, "100.02")],
        [invoice(7, "200.02")],
    )

    assert report["duplicate_charges"]["violations"] == 1
    assert report["lost_payments"]["violations"] == 1


def test_invoice_balance_must_match_captured_payments():
    report = check_payments(
        [payment(1, 7, "100.01", "ch1"), payment(2, 7, "50.00", None, status="PENDING")],
        [charge("ch1", 7, "100.01")],
        [invoice(7, "150.01")],
    )

    assert report["invoice_mismatch"]["violations"] == 1
    assert report["unfinished_payments"]["violations"] == 1


def test_recorded_payment_unknown_to_bank_is_phantom():
    report = check_payments([payment(1, 7, "1.00", "ch9")], [], [invoice(7, "1.00")])

    assert report["phantom_payments"]["violations"] == 1


def test_overbooking_and_counter_drift():
    report = check_sections(
        [
            {"id": 1, "capacity": 30, "enrolled": 5, "registered": 60},
            {"id": 2, "capacity": 30, "enrolled": 10, "registered": 10},
        ]
    )

    assert report["overbooked_sections"] == {"violations": 1, "extra_seats": 30}
    assert report["seat_counter_drift"] == {"violations": 1, "total_abs_drift": 55}


def test_document_is_lost_only_when_no_copy_is_correct(tmp_path):
    data = b'{"gpa": "3.00"}'
    digest = hashlib.sha256(data).hexdigest()
    disk1, disk2 = tmp_path / "d1", tmp_path / "d2"
    for disk in (disk1, disk2):
        (disk / "1").mkdir(parents=True)
    (disk1 / "1" / "good.json").write_bytes(data)
    (disk2 / "1" / "good.json").write_bytes(b"corrupted")
    documents = [
        {"id": 1, "storage_key": "1/good.json", "sha256": digest},
        {"id": 2, "storage_key": "1/missing.json", "sha256": digest},
    ]

    report = check_documents(documents, [disk1, disk2])

    assert report["lost_documents"] == {"violations": 1}
    assert report["bad_copies"] == {"violations": 3}


def test_grade_batch_must_be_complete_or_absent():
    assert check_grade_batch(100, 200)["partial_batch"] == {"violations": 1}
    assert check_grade_batch(200, 200)["partial_batch"] == {"violations": 0}
    assert check_grade_batch(0, 200)["partial_batch"] == {"violations": 0}
    assert check_grade_batch(0, None)["partial_batch"] == {"violations": 0}
