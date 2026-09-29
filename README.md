# Fault-tolerant university information system

AITU, Fault Tolerance and Reliability, midterm project.
Serikbekov Nurzhan Erlanuly (255149, CSE-2505M).

A small distributed university platform (student registration, tuition payments, academic
records and timetable generation) built twice: a baseline without fault-tolerance mechanisms
and a fault-tolerant version. Both are exposed to the same injected failures and compared by
measured reliability and availability.

Status: the baseline and the experiment tooling are ready; the fault-tolerant version and
the final experiments are in progress.

## Services

| Service | Responsibility | Calls |
|---|---|---|
| gateway (HAProxy) | single entry point, path routing | all services |
| student | students, course catalogue, sections, course registration | payment, timetable |
| payment | tuition invoices and payments | bank |
| records | grades, GPA, transcript documents | - |
| timetable | rooms, timeslots, timetable generation jobs | student |
| bank | simulated external payment provider with its own SQLite storage | - |
| pg-1 (PostgreSQL) | one database, one schema per service | - |
| toxiproxy | network fault injection on student -> payment, student -> timetable and payment -> bank | - |

Service-to-service calls go through the gateway; toxiproxy sits on those paths and passes
traffic unchanged until an experiment adds a fault. Every response carries an `X-Instance`
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

## Experiments

The `lab` package runs controlled failure-injection experiments. The host script starts a
fresh stack for every run and then runs the experiment inside a `lab` container on the stack
network, so the load, the service logs and the Docker events share one clock.

```
python -m lab.orchestrate --mode baseline --scenarios E1,E2 --reps 5 --tag final
python -m lab.summarize --tag final
python -m lab.recompute --tag final     # re-evaluate stored runs after a metric change
```

Each run writes `results/<tag>/<mode>/<scenario>/rep<k>/`: every request attempt
(`requests.csv.gz`), the executed actions, Docker events, service logs, the consistency
report and `metrics.json`. `meta.json` records the git commit of the code under test.

| Scenario | Failure | How it is injected |
|---|---|---|
| E1 | application crash | SIGKILL of `payment-1` |
| E2 | database failure | SIGKILL of the primary database |
| E3 | service-to-service timeout | toxiproxy stops answering on student -> payment |
| E4 | node failure | SIGKILL of every container of simulated node `a` |
| E5a | interrupted payment | `payment-1` crashes after the bank charge, before the payment is saved |
| E5b | interrupted grade import | `records-1` crashes half way through a 200-grade batch |
| E5c | interrupted timetable generation | `timetable-1` crashes half way through a job |
| E6 | high load | registration rush: 50 -> 300 requests per second for 60 s (baseline knee: 250-300) |
| CAL, CAL_RUSH | none | step load to find the capacity of the baseline (default and registration mix) |

The load is open-loop: requests start on a fixed schedule whatever the response times, so a
failing system does not reduce the offered load. The plan of requests is built up front from
a seed (the repetition number) and split between four worker processes, so the generator
keeps its schedule even when thousands of requests wait for their timeouts. The mix covers
every service; a failed payment is retried twice by the client with the same idempotency
key, like a user pressing "Pay" again. A failure is injected at 20 s. At 60 s (40 s for E5) a `repair` action restarts
whatever was killed and removes network faults, modelling an operator; both versions get the
same repair at the same time.

Metrics (definitions in `lab/metrics.py`, notation of the course lectures):

- a request is good if the system answered it without a server error within 2 s (latency
  SLO); 4xx business rejections count as answers, 5xx, 429, slow answers, timeouts (5 s) and
  connection errors do not;
- a second is unavailable when less than 95% of the requests sent in it were good; a run of
  unavailable seconds is one outage, D_i is its length;
- A = U_total / T_obs, MTTF = U_total / N, MTTR = D_total / N, MTBF = T_obs / N;
- detection time: from the injection to the first sign that the system itself noticed the
  failure (health check, circuit breaker, failover); recovery time: from the injection to the
  end of the last outage.

Consistency checks after every run compare the bank with the payment records (lost payments,
double charges, invoice balances), sections with registrations (overbooking, counter drift),
transcript files with their stored hashes, and look for half-imported grade batches and
timetable jobs stuck in `RUNNING`.

Code paths where a crash matters are marked with `fault_point(...)`; the lab arms them through
the `/_chaos` endpoint, which is mounted only when `UFT_CHAOS_ENABLED=true` and is not routed
by the gateway.

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
| `infra/haproxy`, `infra/toxiproxy` | gateway and network fault proxy configuration |
| `lab` | load generator, failure injection, consistency checks, metrics, scenarios |
| `results` | experiment datasets |
| `tests/unit`, `tests/integration` | unit tests and end-to-end smoke tests |
