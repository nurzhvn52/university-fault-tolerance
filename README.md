# Fault-tolerant university information system

AITU, Fault Tolerance and Reliability, midterm project.
Serikbekov Nurzhan Erlanuly (255149, CSE-2505M).

A small distributed university platform (student registration, tuition payments, academic
records and timetable generation) built twice: a baseline without fault-tolerance mechanisms
and a fault-tolerant version. Both are exposed to the same injected failures and compared by
measured reliability and availability.

Status: the baseline is ready; the fault-tolerant version, failure injection and experiments
are in progress.

## Services

| Service | Responsibility | Calls |
|---|---|---|
| gateway (HAProxy) | single entry point, path routing | all services |
| student | students, course catalogue, sections, course registration | payment, timetable |
| payment | tuition invoices and payments | bank |
| records | grades, GPA, transcript documents | - |
| timetable | rooms, timeslots, timetable generation jobs | student |
| bank | simulated external payment provider with its own SQLite storage | - |
| postgres | one database, one schema per service | - |

Service-to-service calls go through the gateway. Every response carries an `X-Instance`
header with the container that served it.

## Run the baseline

Requires Docker with Compose v2.

```
docker compose -f docker-compose.baseline.yml up -d --build
curl localhost:8080/api/students/1
```

A one-shot `migrate` job applies the Alembic migrations and loads deterministic synthetic
data (600 students, 40 courses, 120 sections, invoices, grades and an initial timetable).
HAProxy statistics are at http://localhost:8404/stats.

Main endpoints:

| Method and path | What it does |
|---|---|
| `GET /api/students/{id}` | student profile |
| `GET /api/sections?term=2026-FALL` | sections with capacity and enrolment |
| `POST /api/registrations` | register for a section (checks tuition, schedule and seats) |
| `GET /api/tuition/{student_id}?term=2026-FALL` | tuition balance |
| `POST /api/payments` | pay tuition through the bank |
| `GET /api/transcripts/{student_id}` | grades and GPA |
| `POST /api/transcripts/{student_id}/documents` | generate and store a transcript document |
| `POST /api/timetable/generate?term=2026-FALL` | start a timetable generation job |

## Known weaknesses of the baseline

The baseline is written the way a first version often is. These weaknesses are kept on
purpose, the experiments measure them:

- no client timeouts on calls between services (the gateway gives up only after 5 minutes)
  and no statement timeouts on database queries, so a slow dependency blocks requests and
  holds database connections;
- one instance of everything and no restart policy, so any crash is an outage until a person
  restarts the container;
- a payment is recorded only after the bank charged the student, without an idempotency key:
  a crash in between loses the record, and a retry charges the student twice;
- the seat counter of a section is updated by read-modify-write, so concurrent registrations
  can overbook a section and the counter drifts;
- grade batch import commits row by row, so an interrupted import is left half done;
- timetable generation keeps its progress only in memory, and a crash leaves the job stuck
  in `RUNNING`;
- transcript files are stored once, and their hash is never checked on read.

## Development

```
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest                                          # unit tests
UFT_BASE_URL=http://localhost:8080 pytest -m integration   # needs the running stack
```

## Repository layout

| Path | Content |
|---|---|
| `services/common` | settings, JSON logging, database session, health endpoints, app factory |
| `services/<service>` | one package per service |
| `services/tools/seed.py` | synthetic data |
| `migrations` | Alembic migrations for all schemas |
| `infra/haproxy` | gateway configuration |
| `tests/unit`, `tests/integration` | unit tests and end-to-end smoke tests |
