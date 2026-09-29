"""Deterministic synthetic data for the university database.

Run after the migrations: ``python -m tools.seed``. Does nothing if students already exist.
All names and numbers are generated from a fixed random seed; there is no real personal data.
"""

import asyncio
import logging
import random
from decimal import Decimal

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from common.config import get_settings
from common.logs import configure_logging
from payment.models import Invoice
from records.gpa import GRADE_POINTS
from records.models import Grade
from student.models import Course, Section, Student
from timetable.models import Room, Timeslot, TimetableEntry
from timetable.scheduler import RoomInfo, Scheduler, SectionDemand, order_sections

logger = logging.getLogger("tools.seed")

SEED = 42
STUDENTS = 600
SECTIONS_PER_COURSE = 3
PAID_SHARE = 0.7
TUITION = Decimal("1500000.00")
PAST_TERMS = ["2023-FALL", "2024-SPRING", "2024-FALL", "2025-SPRING", "2025-FALL", "2026-SPRING"]
COURSES_PER_TERM = 4

FIRST_NAMES = {
    "m": [
        "Arman",
        "Yerlan",
        "Daniyar",
        "Timur",
        "Askar",
        "Bolat",
        "Ilyas",
        "Rustem",
        "Samat",
        "Nurlan",
        "Azamat",
        "Marat",
        "Olzhas",
        "Dias",
        "Alikhan",
    ],
    "f": [
        "Aigerim",
        "Dana",
        "Madina",
        "Aruzhan",
        "Kamila",
        "Saule",
        "Zhanna",
        "Ayana",
        "Dinara",
        "Aliya",
        "Togzhan",
        "Assel",
        "Aizhan",
        "Zarina",
        "Laura",
    ],
}
LAST_NAMES = [
    "Abenov",
    "Omarov",
    "Serikov",
    "Tulegenov",
    "Kassymov",
    "Nurlanov",
    "Ibraimov",
    "Mukhanov",
    "Zhaksylykov",
    "Akhmetov",
    "Bekov",
    "Sadykov",
    "Karimov",
    "Temirov",
    "Yessenov",
    "Baimukhanov",
    "Dzhaksybekov",
    "Ermekov",
    "Suleimenov",
    "Utepov",
]
PROGRAMS = [
    "Software Engineering",
    "Computer Science",
    "Cybersecurity",
    "Big Data Analysis",
    "IT Management",
]
COURSE_TITLES = [
    ("CSE101", "Programming Fundamentals"),
    ("CSE102", "Object-Oriented Programming"),
    ("CSE201", "Data Structures"),
    ("CSE202", "Algorithms"),
    ("CSE203", "Computer Architecture"),
    ("CSE204", "Operating Systems"),
    ("CSE205", "Computer Networks"),
    ("CSE206", "Databases"),
    ("CSE301", "Software Engineering"),
    ("CSE302", "Web Development"),
    ("CSE303", "Mobile Development"),
    ("CSE304", "Distributed Systems"),
    ("CSE305", "Cloud Computing"),
    ("CSE306", "Information Security"),
    ("CSE307", "Cryptography"),
    ("CSE308", "Machine Learning"),
    ("CSE309", "Deep Learning"),
    ("CSE310", "Data Mining"),
    ("CSE311", "Computer Graphics"),
    ("CSE312", "Human-Computer Interaction"),
    ("CSE313", "Compilers"),
    ("CSE314", "Theory of Computation"),
    ("CSE315", "Parallel Programming"),
    ("CSE316", "Fault Tolerance and Reliability"),
    ("CSE317", "Software Testing"),
    ("CSE318", "DevOps Practices"),
    ("MAT101", "Calculus I"),
    ("MAT102", "Calculus II"),
    ("MAT103", "Linear Algebra"),
    ("MAT104", "Discrete Mathematics"),
    ("MAT201", "Probability and Statistics"),
    ("PHY101", "Physics I"),
    ("PHY102", "Physics II"),
    ("GEN101", "Academic English"),
    ("GEN102", "Kazakh Language"),
    ("GEN103", "History of Kazakhstan"),
    ("GEN104", "Philosophy"),
    ("GEN105", "Economics"),
    ("GEN106", "Entrepreneurship"),
    ("GEN107", "Project Management"),
]
ROOM_CAPACITIES = [30, 30, 40, 40, 50, 50, 60, 60, 80, 80, 100, 100, 120, 150, 200]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
SLOT_TIMES = [
    ("08:00", "09:20"),
    ("09:30", "10:50"),
    ("11:00", "12:20"),
    ("13:00", "14:20"),
    ("14:30", "15:50"),
]
LETTERS = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]
LETTER_WEIGHTS = [12, 12, 14, 16, 12, 10, 9, 6, 4, 3, 2]


def make_rows(rng: random.Random, term: str) -> dict[type, list[dict]]:
    students = []
    for i in range(1, STUDENTS + 1):
        gender = rng.choice("mf")
        last = rng.choice(LAST_NAMES) + ("a" if gender == "f" else "")
        students.append(
            {
                "id": i,
                "student_no": f"{250000 + i}",
                "full_name": f"{rng.choice(FIRST_NAMES[gender])} {last}",
                "program": rng.choice(PROGRAMS),
                "year": rng.randint(1, 4),
            }
        )

    courses = [
        {"id": i, "code": code, "title": title, "credits": rng.choice([4, 5, 6])}
        for i, (code, title) in enumerate(COURSE_TITLES, 1)
    ]
    sections = [
        {
            "id": len(courses) * k + course["id"],
            "course_id": course["id"],
            "term": term,
            "capacity": rng.choice([30, 40, 50, 60]),
        }
        for k in range(SECTIONS_PER_COURSE)
        for course in courses
    ]

    invoices = []
    for student in students:
        paid = TUITION if rng.random() < PAID_SHARE else Decimal("0.00")
        invoices.append(
            {
                "student_id": student["id"],
                "term": term,
                "amount_due": TUITION,
                "opening_paid": paid,
                "amount_paid": paid,
            }
        )

    grades = []
    for student in students:
        # A student in year N has 2 * (N - 1) finished terms.
        for past_term in PAST_TERMS[len(PAST_TERMS) - 2 * (student["year"] - 1) :]:
            for course in rng.sample(courses, COURSES_PER_TERM):
                letter = rng.choices(LETTERS, LETTER_WEIGHTS)[0]
                grades.append(
                    {
                        "student_id": student["id"],
                        "course_code": course["code"],
                        "term": past_term,
                        "letter": letter,
                        "points": GRADE_POINTS[letter],
                        "credits": course["credits"],
                    }
                )

    rooms = [
        {"id": i, "code": f"C1.{100 + i}", "capacity": capacity}
        for i, capacity in enumerate(ROOM_CAPACITIES, 1)
    ]
    timeslots = [
        {"id": d * len(SLOT_TIMES) + s + 1, "day": day, "starts_at": start, "ends_at": end}
        for d, day in enumerate(DAYS)
        for s, (start, end) in enumerate(SLOT_TIMES)
    ]

    scheduler = Scheduler(
        [RoomInfo(room["id"], room["capacity"]) for room in rooms],
        [slot["id"] for slot in timeslots],
    )
    entries = []
    for demand in order_sections(SectionDemand(s["id"], s["capacity"]) for s in sections):
        room_id, timeslot_id = scheduler.place(demand)
        entries.append(
            {
                "term": term,
                "section_id": demand.section_id,
                "room_id": room_id,
                "timeslot_id": timeslot_id,
            }
        )

    return {
        Student: students,
        Course: courses,
        Section: sections,
        Invoice: invoices,
        Grade: grades,
        Room: rooms,
        Timeslot: timeslots,
        TimetableEntry: entries,
    }


async def reset_sequences(conn: AsyncConnection, tables: list[str]) -> None:
    """Explicit ids were inserted, so move each id sequence past the largest id."""
    for table in tables:
        await conn.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT coalesce(max(id), 1) FROM {table}))"
            )
        )


async def main() -> None:
    settings = get_settings()
    configure_logging("seed", settings.node, settings.log_level)
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            existing = await conn.scalar(select(func.count()).select_from(Student))
            if existing:
                logger.info("seed_skipped", extra={"students": existing})
                return
            rows = make_rows(random.Random(SEED), settings.term)
            for model, model_rows in rows.items():
                await conn.execute(insert(model), model_rows)
            await reset_sequences(conn, [model.__table__.fullname for model in rows])
        logger.info(
            "seed_completed", extra={model.__tablename__: len(r) for model, r in rows.items()}
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
