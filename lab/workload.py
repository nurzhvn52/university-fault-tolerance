"""Request mix of the load generator: which operations, how often and with what data."""

import random
import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

TERM = "2026-FALL"

SERVICE_OF = {
    "student.get": "student",
    "sections.list": "student",
    "registration.create": "student",
    "tuition.get": "payment",
    "payment.create": "payment",
    "transcript.get": "records",
    "document.create": "records",
    "document.get": "records",
    "timetable.slots": "timetable",
}

# Relative weights of the operations (reads dominate, like a real student portal).
DEFAULT_MIX = {
    "student.get": 15,
    "sections.list": 5,
    "registration.create": 20,
    "tuition.get": 15,
    "payment.create": 10,
    "transcript.get": 15,
    "document.create": 5,
    "document.get": 5,
    "timetable.slots": 10,
}

# Every payment of a student gets a different amount (base + n cents), so two bank charges
# with the same account and amount can only come from one payment being charged twice.
PAYMENT_BASE = Decimal("50000.00")
PAYMENT_RETRIES = 2


@dataclass(frozen=True)
class Request:
    op: str
    method: str
    path: str
    params: dict | None = None
    json: dict | None = None
    headers: dict | None = None
    # Payment intent: all attempts of one payment share it (and its idempotency key).
    intent: str = ""
    # How many times the client repeats a failed attempt, like a user pressing "Pay" again.
    retries: int = 0

    @property
    def service(self) -> str:
        return SERVICE_OF[self.op]


@dataclass
class Population:
    students: int
    paid: list[int]
    unpaid: list[int]
    with_grades: list[int]
    sections: list[int]


@dataclass
class Workload:
    population: Population
    seed: int
    mix: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MIX))

    def __post_init__(self) -> None:
        unknown = set(self.mix) - set(SERVICE_OF)
        if unknown:
            raise ValueError(f"unknown operations in mix: {sorted(unknown)}")
        self._rng = random.Random(self.seed)
        self._ops = list(self.mix)
        self._weights = list(self.mix.values())
        self._payments: Counter[int] = Counter()

    def next(self) -> Request:
        op = self._rng.choices(self._ops, self._weights)[0]
        return getattr(self, "_" + op.replace(".", "_"))()

    def payment_amount(self, student_id: int) -> Decimal:
        self._payments[student_id] += 1
        return PAYMENT_BASE + Decimal(self._payments[student_id]) / 100

    def _pick(self, ids: list[int]) -> int:
        return self._rng.choice(ids)

    def _student_get(self) -> Request:
        student_id = self._rng.randint(1, self.population.students)
        return Request("student.get", "GET", f"/api/students/{student_id}")

    def _sections_list(self) -> Request:
        return Request("sections.list", "GET", "/api/sections", params={"term": TERM})

    def _registration_create(self) -> Request:
        body = {
            "student_id": self._pick(self.population.paid),
            "section_id": self._pick(self.population.sections),
        }
        return Request("registration.create", "POST", "/api/registrations", json=body)

    def _tuition_get(self) -> Request:
        student_id = self._rng.randint(1, self.population.students)
        return Request("tuition.get", "GET", f"/api/tuition/{student_id}", params={"term": TERM})

    def _payment_create(self) -> Request:
        student_id = self._pick(self.population.unpaid)
        intent = uuid.UUID(int=self._rng.getrandbits(128)).hex
        body = {
            "student_id": student_id,
            "term": TERM,
            "amount": str(self.payment_amount(student_id)),
        }
        return Request(
            "payment.create",
            "POST",
            "/api/payments",
            json=body,
            headers={"Idempotency-Key": intent},
            intent=intent,
            retries=PAYMENT_RETRIES,
        )

    def _transcript_get(self) -> Request:
        student_id = self._pick(self.population.with_grades)
        return Request("transcript.get", "GET", f"/api/transcripts/{student_id}")

    def _document_create(self) -> Request:
        student_id = self._pick(self.population.with_grades)
        return Request("document.create", "POST", f"/api/transcripts/{student_id}/documents")

    def _document_get(self) -> Request:
        student_id = self._pick(self.population.with_grades)
        return Request("document.get", "GET", f"/api/transcripts/{student_id}/documents/latest")

    def _timetable_slots(self) -> Request:
        ids = self._rng.sample(self.population.sections, 3)
        return Request(
            "timetable.slots",
            "GET",
            "/api/timetable/sections",
            params={"term": TERM, "ids": ",".join(map(str, ids))},
        )
