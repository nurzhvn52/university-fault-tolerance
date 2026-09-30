# Fault-tolerant university information system

AITU, Fault Tolerance and Reliability, midterm project.
Serikbekov Nurzhan Erlanuly (255149, CSE-2505M).

A small distributed university platform (student registration, tuition payments, academic
records and timetable generation) built twice: a baseline without fault-tolerance mechanisms
and a fault-tolerant version. Both are exposed to the same injected failures and compared by
measured reliability and availability.

Status: the baseline, the fault-tolerant version (software mechanisms and infrastructure
redundancy) and the experiment tooling are ready; the final experiments and the report are
in progress.

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

## Software fault-tolerance mechanisms

Switched on with `UFT_FT_MODE=ft` (same code, the baseline paths stay untouched):

```
UFT_FT_MODE=ft docker compose -f docker-compose.baseline.yml up -d --build
```

| Mechanism | Where | Protects against |
|---|---|---|
| Timeouts on every call and query | `common/resilience.py` (`Dependency`), `common/db.py` | a slow dependency blocking requests |
| Retry with exponential backoff and full jitter, only for idempotent calls | `common/resilience.py` | transient network and service errors |
| Circuit breaker per dependency (5 failures open it, trial call after 5 s) | `common/resilience.py` | waiting on a dependency that is down |
| Load shedding (at most 48 requests in flight per instance, then 503) | `common/app.py` | overload turning into timeouts for everyone |
| Bulkhead: no database connection held while other services are called | `student/registration.py` | one slow dependency exhausting the pool |
| Idempotency keys and duplicate-request detection | `payment/journal.py` | double charges on client retries |
| Journal (PENDING before the bank call), roll-forward recovery worker | `payment/journal.py` | a crash between the bank charge and the record |
| Atomic, idempotent batch import (one transaction, upsert) | `records/resilient.py` | half-imported batches |
| Checkpointing and leases of timetable jobs, resume after a crash | `timetable/checkpointed.py` | lost work and jobs stuck in RUNNING |
| Recovery block (acceptance test, first-fit alternate) | `timetable/scheduler.py` | a failed or poor primary timetable |
| N-version programming: GPA by three independent versions and a majority voter | `records/gpa.py` | a design fault in one implementation |
| Graceful degradation: registrations accepted as PENDING_VERIFICATION, cached timetable, stale transcripts | `student/registration.py`, `records/resilient.py` | payment, timetable or database outages |
| Atomic seat reservation (`enrolled < capacity` in the UPDATE) | `student/registration.py` | overbooking and lost counter updates under load |
| Health endpoints with timeouts, readiness without the database for records | `common/health.py` | routing traffic to an instance that cannot serve |
| Fast 503 with Retry-After for unavailable dependencies and database | `common/app.py` | hanging requests and unclear errors |

Every mechanism logs its activity (`breaker_opened`, `retry_succeeded`, `payment_reconciled`,
`fallback_used`, `job_resumed`, ...); the experiments count these events.

## Infrastructure (hardware) fault tolerance

The fault-tolerant stack runs every service twice, on two simulated nodes:

```
docker compose -f docker-compose.ft.yml up -d --build
docker compose -f docker-compose.ft.yml --profile monitoring up -d   # plus Prometheus and Grafana
```

| Node | Containers |
|---|---|
| a | student-1, payment-1, records-1, timetable-1, pg-1 (Patroni, first primary), etcd-1 |
| b | student-2, payment-2, records-2, timetable-2, pg-2 (Patroni, synchronous standby), etcd-2 |
| c | etcd-3 (third quorum member), watchdog, backup, monitoring |
| edge | gateway (HAProxy) |

| Mechanism | How | Protects against |
|---|---|---|
| Active redundancy of services | two replicas per service behind HAProxy, round robin | the loss of one instance or one node |
| Health checks and failover at the gateway | `/health/ready` every 1 s, DOWN after 2 failures, retry and redispatch of safe requests | routing to a dead or unready replica |
| Passive (hot standby) redundancy of the database | PostgreSQL streaming replication, synchronous commit to the standby (RPO 0) | loss of the primary database |
| Leader election and automatic failover | Patroni with a 3-member etcd cluster (Raft); HAProxy routes to the node that answers `/primary` | two primaries (split brain), manual failover |
| Rejoin of the old primary | `pg_rewind` when the failed node comes back | a node that cannot return after a failover |
| Restart policy | `restart: on-failure` for every service | crashed processes |
| Watchdog | `tools/watchdog.py` restarts containers whose liveness check fails | hung processes |
| Mirrored storage with voting (RAID-1 / TMR) | three copies of every transcript file, majority read, read repair, periodic scrub | corrupted or lost files on one disk |
| Backups | `pg_dump` every 60 s, `infra/backup/restore_check.sh` measures restore time and backup age | loss of both database nodes, operator errors |
| ECC (simulated) | `lab/ecc.py`: SEC-DED (72,64) as in ECC memory, compared with parity by bit-flip injection (`python -m lab.ecc_experiment`) | bit flips in memory |

The gateway is the remaining single point of failure; in production it would be doubled with
a floating IP (keepalived) or a managed load balancer.

Monitoring: HAProxy statistics at http://localhost:8404/stats, Prometheus at
http://localhost:9090, Grafana (dashboard "University system - fault tolerance") at
http://localhost:3000.

## Experiments

The `lab` package runs controlled failure-injection experiments. The host script starts a
fresh stack for every run and then runs the experiment inside a `lab` container on the stack
network, so the load, the service logs and the Docker events share one clock.

```
python -m lab.orchestrate --mode baseline --scenarios E1,E2 --reps 5 --tag final
python -m lab.orchestrate --mode sw --scenarios E1,E2 --reps 5 --tag final   # software mechanisms only
python -m lab.orchestrate --mode ft --scenarios E1,E2 --reps 5 --tag final   # software + infrastructure
python -m lab.summarize --tag final
python -m lab.recompute --tag final     # re-evaluate stored runs after a metric change
```

Each run writes `results/<tag>/<mode>/<scenario>/rep<k>/`: every request attempt
(`requests.csv.gz`), the executed actions, Docker events, service logs, the consistency
report and `metrics.json`. `meta.json` records the git commit of the code under test.

| Scenario | Failure | How it is injected |
|---|---|---|
| E1 | application crash | the process of `payment-1` exits (137) |
| E1b | application hang | the event loop of `payment-1` blocks, the process stays alive |
| E2 | database failure | SIGKILL of the primary database |
| E3 | service-to-service timeout | toxiproxy stops answering on student -> payment |
| E4 | node failure | SIGKILL of every container of simulated node `a` |
| E5a | interrupted payment | `payment-1` crashes after the bank charge, before the payment is saved |
| E5b | interrupted grade import | `records-1` crashes half way through a 200-grade batch |
| E5c | interrupted timetable generation | `timetable-1` crashes half way through a job |
| E6 | high load | registration rush: 50 -> 300 requests per second for 60 s (baseline knee: 250-300) |
| E9 | storage corruption and disk failure | every transcript file on disk 1 corrupted, then disk 2 wiped |
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
