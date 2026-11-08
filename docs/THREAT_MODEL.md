# Threat model

Scope: the university system in the endterm configuration (gateway, auth, student, payment,
records and timetable services, the simulated bank, PostgreSQL with Patroni, monitoring). Data
is synthetic; the lab stack is never exposed to a real network. The point is to show which
attacks the design tolerates and how that is verified, not to certify a production system.

## Assets

| Asset | Why it matters | Where |
|---|---|---|
| Grades and transcripts | integrity of academic records, privacy | records schema, transcript files |
| Payments and invoices | money; no lost or doubled charges | payment schema, bank |
| Registrations and seat counters | fairness of registration | student schema |
| Credentials and signing keys | everything else depends on them | auth schema, `secrets/` |
| Availability of registration and payment | peak load, deadlines | all services |
| Audit trail | accountability, incident reconstruction | `audit.events`, JSON logs |

## Actors and assumptions

| Actor | Capability | Assumed |
|---|---|---|
| Anonymous outsider | sends any HTTP request to the gateway | has no token |
| Student (insider) | valid student token, can script requests | wants other students' data or free actions |
| Compromised client | steals one student's token | token valid until it expires (15 min) |
| Compromised service | code execution inside one service container | has that service's secrets and database role |
| Operator | uses staff tokens | trusted, but audited |

Trust boundaries: (1) internet to gateway; (2) gateway to services; (3) services to the
database; (4) payment to the external bank. Out of scope: host or Docker compromise, attacks on
PostgreSQL or HAProxy themselves, TLS (the lab uses plain HTTP inside one Docker network; a
real deployment terminates TLS at the gateway and uses mTLS or a service mesh internally),
Byzantine failures of replicas.

## Threats, controls and verification

STRIDE: S spoofing, T tampering, R repudiation, I information disclosure, D denial of service,
E elevation of privilege.

| ID | Threat | Cat. | Control | Evidence |
|---|---|---|---|---|
| T1 | Request without credentials or with a forged, unsigned or expired token | S | JWT HS256 with `iss`, `aud`, `exp`, `jti` required; only HS256 accepted (`alg=none` refused); signature checked by every service | unit tests, X7 |
| T2 | Student reads or changes another student's data (IDOR) by changing an id in the path, query or body | I, T | ownership check against the `sid` claim on every student-scoped route, also for ids found through a payment or registration | integration tests, X7 |
| T3 | Student calls a staff operation (grade import, timetable generation, invoice list) | E | role check (`registrar`, `admin`); service tokens cannot create payments or call staff routes | X7 |
| T4 | Brute force or credential stuffing on login | S | scrypt hashes, account lock after 5 failures (30 s), gateway limit of 30 login attempts per 10 s per address, identical answer for unknown user and wrong password, equal verification time | X9 |
| T5 | Malformed or hostile input (bad JSON, injection strings, negative or huge amounts, unknown fields such as `role`) | T, E | Pydantic models with patterns and bounds, `extra=forbid` for bodies, parameterised queries only, 422 before any handler logic | X8 |
| T6 | Oversized or slow requests | D | 256 KiB body limit at the gateway and in the service, `Content-Length` required for writes, 5 s limit for request headers (slowloris) | X8 |
| T7 | One client floods the API and starves the others | D | token bucket per principal (students 20 req/s, burst 40; staff ten times that) answering 429 with `Retry-After`; load shedding per instance; HAProxy health checks | X8, X4 |
| T8 | Compromised service reads another service's data | I | one database role per service with rights only on its own schema; INSERT-only on the audit table | X10 |
| T9 | Compromised service or client abuses internal APIs | E | service tokens (5 min) instead of end-user tokens for internal calls; the bank accepts only its API key and never sees an internal token; `/_chaos`, `/metrics` and `/health` are not routed by the gateway | X7, tests |
| T10 | Stolen signing key or default secret | S | secrets from files, never in the image or git; services refuse to start with a short or default secret; key rotation by listing the old key as verify-only | unit tests, `lab.init_secrets --rotate` |
| T11 | Repudiation: nobody can tell who did what | R | audit events (actor, action, resource, outcome, request id) for logins, denials, throttling and every write; JSON log is authoritative, database copy is buffered through database outages | X7, X3 |
| T12 | Attack goes unnoticed | all | alerts `AuthFailureBurst`, `RateLimitAbuse`, `HighErrorRate`; request id links an alert to log lines of every service | X5, X7, X8 |
| T13 | Double charge or lost payment when a request is repeated or interrupted | T | idempotency keys and payment journal from the midterm (kept) | X6 |
| T14 | Replay of a stolen token | S | not prevented before expiry; short lifetime limits the window; every use is audited | residual risk |

## Safe failure behaviour

- Authentication and authorization fail closed: no token, a bad token or a rule that cannot be
  evaluated ends in 401 or 403, never in an implicit allow.
- Tokens are verified locally, so a failure of the auth service or the database does not turn
  into mass denial and does not open anything: holders of valid tokens keep working, new logins
  fail with 503.
- Audit is fail-open on purpose: if the database is down, business requests are still served
  and the events wait in a bounded buffer (5000) and in the JSON log. If the buffer overflows,
  the oldest events are dropped and counted (`uft_audit_dropped_total`).
- The rate limiter keeps its state in memory per instance, so a restart resets it and two
  replicas allow up to twice the nominal rate.

## Residual risks

- No token revocation before expiry (a deleted or locked user keeps a valid token for up to
  15 minutes).
- Rate limiting is per replica; a distributed attacker with many identities is limited only by
  load shedding and the gateway.
- The per-address login limit sees the gateway's view of the source address; clients behind one
  NAT share it.
- No TLS, no mTLS between services, no WAF.
- One gateway instance (single point of failure, from the midterm).
- `/_chaos` exists in lab builds (`UFT_CHAOS_ENABLED=true`); it must be off in production.
