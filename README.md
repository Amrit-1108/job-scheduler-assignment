# Job Scheduler & Execution Engine

A small scheduling service: you POST a job, a pool of workers picks it up when it's
due, runs it, retries it if it fails, and never runs the same occurrence twice.

FastAPI + SQLAlchemy + PostgreSQL, with a separate worker process. One database
engine, everywhere - the tests run against Postgres too, so the claim path is
genuinely exercised rather than quietly skipped by a lighter backend.

## Running it

```bash
docker compose up --build
```

That brings up Postgres, the API on <http://localhost:8000> and two workers.
Swagger UI is at <http://localhost:8000/docs>.

The database container is published on host port **5433**, not 5432, so it doesn't
collide with a Postgres you already have installed. Inside the compose network the
services still talk to `db:5432`.

Add more workers whenever you want:

```bash
docker compose up --build --scale worker=5
```

Locally without Docker:

```bash
pip install -r requirements.txt

# create the database once, then point the app at it
#   createdb -U postgres scheduler
export DATABASE_URL="postgresql+psycopg2://postgres:YOUR_POSTGRES_PASSWORD@localhost:5432/scheduler"

uvicorn app.main:app --reload            # terminal 1
python -m app.worker                     # terminal 2 (repeat for more workers)
```

Tests:

```bash
pytest
```

They need a reachable Postgres and create their own `scheduler_test` database on first
run. Point `TEST_DATABASE_URL` elsewhere if yours doesn't live on localhost:5432.

## Auth

Every `/jobs` endpoint needs a bearer token; `/health` and the docs stay open.

```bash
curl -X POST localhost:8000/auth/register \
  -H 'content-type: application/json' \
  -d '{"username": "ashish", "password": "YOUR_ACCOUNT_PASSWORD"}'

TOKEN=$(curl -s -X POST localhost:8000/auth/token \
  -d 'username=ashish&password=YOUR_ACCOUNT_PASSWORD' | jq -r .access_token)

curl localhost:8000/jobs -H "Authorization: Bearer $TOKEN"
```

In Swagger: call `/auth/token`, copy the `access_token`, then paste it into the
**Authorize** box at the top right. Every subsequent call in the page is signed.

Login takes exactly a username and a password. FastAPI's `OAuth2PasswordRequestForm`
would have added `grant_type`, `scope`, `client_id` and `client_secret` to the form -
spec baggage this service doesn't use - so the endpoint takes a plain JSON body and
the docs use a plain bearer scheme.

Passwords are bcrypt-hashed (never stored or echoed), tokens are HS256 JWTs with a
one-hour expiry, and **jobs are owned**: `Job.owner_id` is set from the token and every
read and write is filtered by it. Reaching for someone else's job returns **404, not
403** — a 403 would confirm the id exists and hand an attacker an enumeration oracle.
Idempotency keys are unique per user for the same reason.

What this deliberately is not: no refresh tokens, no revocation list, no roles, no
password reset. It's session-grade auth for a service API. Set `JWT_SECRET` to
something random in any real deployment — the app logs a warning if you leave the
built-in dev value, and all API replicas must share the same secret.

## API

| Method | Path                    | What it does                                            |
| ------ | ----------------------- | ------------------------------------------------------- |
| POST   | `/auth/register`        | Create an account                                       |
| POST   | `/auth/token`           | Log in, get a JWT                                       |
| GET    | `/auth/me`              | Current user                                            |
| POST   | `/jobs`                 | Schedule a job (one-off, interval or cron)              |
| GET    | `/jobs`                 | List with filters: status, schedule_type, next run window |
| GET    | `/jobs/{id}`            | Job state, last execution, next run, retries used/left  |
| GET    | `/jobs/{id}/executions` | Full execution history                                  |
| POST   | `/jobs/{id}/pause`      | Stop scheduling it (bonus)                              |
| POST   | `/jobs/{id}/resume`     | Put it back in the queue                                |
| POST   | `/jobs/{id}/cancel`     | Give up on it                                           |
| POST   | `/jobs/{id}/replay`     | Requeue a FAILED / DEAD_LETTER job with a fresh budget  |
| GET    | `/jobs/stats`           | Counts by status                                        |
| GET    | `/health`               | Liveness + DB check                                     |

```bash
# one-off
curl -X POST localhost:8000/jobs -H 'content-type: application/json' -d '{
  "name": "send-invoice",
  "payload": {"task": "simulated", "invoice_id": 42},
  "schedule_type": "one_time",
  "run_at": "2030-01-01T10:00:00Z",
  "max_retries": 3,
  "idempotency_key": "invoice-42"
}'

# every 30 seconds
curl -X POST localhost:8000/jobs -H 'content-type: application/json' -d '{
  "name": "sync-inventory", "schedule_type": "interval", "interval_seconds": 30
}'

# cron
curl -X POST localhost:8000/jobs -H 'content-type: application/json' -d '{
  "name": "nightly-report", "schedule_type": "cron", "cron_expression": "0 2 * * *"
}'
```

## How it's put together

```
app/
  api/          HTTP layer only - routers, request/response schemas, DI wiring
                (get_job_service binds the service to the caller, so no route
                 can forget to scope a query)
  domain/       models, enums, state machine, scheduling maths, errors
  repositories/ every SQL statement that touches jobs / job_executions
  services/     use cases: JobService (CRUD + lifecycle), JobRunner (the engine)
  worker/       the polling loop and its process entrypoint
  core/         config, engine/session, clock, logging
```

The rule I stuck to: the domain layer knows nothing about HTTP or SQLAlchemy sessions,
services don't build SQL, and routers don't contain business rules. That's what makes
the scheduling logic testable without a database — `tests/test_scheduling.py` is pure
function tests, no fixtures.

The API and the worker are separate processes that share nothing but the database.
Scaling either one is just running more copies.

### Job lifecycle

```
                 ┌───────── pause ────────┐
                 ▼                        │
             PAUSED ── resume ──► SCHEDULED ──claim──► RUNNING
                                    ▲   ▲                │
                     retry (backoff)│   │ next occurrence │
                                    └───┴────────────────┤
                                                          ├─► COMPLETED   (one-off, success)
                                                          ├─► FAILED      (retries exhausted)
                                                          └─► DEAD_LETTER (recurring, exhausted)
```

Every transition goes through `app/domain/state_machine.py`, so an illegal move
(resuming a COMPLETED job, say) raises instead of quietly corrupting state. The API
turns that into a 409.

## The three questions from the brief

### Running multiple workers

Workers are identical and stateless — no leader election, no sharding, no coordination
service. Each one loops: reap dead leases, claim a batch of due jobs, run them on a
small thread pool, sleep for the poll interval. `WORKER_CONCURRENCY` caps how many jobs
one worker runs at once, and it only ever claims as many jobs as it has free slots, so
work spreads across the pool instead of piling up in one process' queue.

`docker compose up --scale worker=5` is genuinely all there is to it.

### Avoiding duplicate execution

The claim is the only thing that matters, and it's in
[`JobRepository.claim_due`](app/repositories/jobs.py). Two guards:

1. `SELECT id, version ... WHERE status = 'SCHEDULED' AND next_run_at <= now
   ORDER BY next_run_at LIMIT n FOR UPDATE SKIP LOCKED` — on Postgres, workers walk
   straight past rows someone else is claiming instead of queueing behind them.
2. `UPDATE jobs SET status='RUNNING', locked_by=..., version=version+1
    WHERE id = ? AND status = 'SCHEDULED' AND version = ?` — a compare-and-swap. If
   another worker got there first, `rowcount` is 0 and we simply drop the job.

The second check is what keeps this correct even if the first is weakened — a lower
isolation level, a connection pooler in the middle, or a backend without SKIP LOCKED.
Belt and braces on the one operation that must never be wrong.

The same trick guards the *end* of an execution: `_commit_outcome` writes the result
with `WHERE version = <version we claimed> AND locked_by = <me>`. So if a worker
stalled long enough for the reaper to take the job back, its late result is discarded
instead of stomping on the newer state. `tests/test_concurrency.py` covers exactly that
race.

Duplicate *creation* is handled separately: send an `idempotency_key` and a repeated
POST returns the original job with 200 instead of creating a twin. There's a unique
index behind it, so two simultaneous requests can't both win.

### Making execution durable

- **Leases, not locks.** A claim writes `locked_by` and `lease_expires_at` to the row.
  Nothing is held in memory, so a worker that gets `kill -9`'d or a container that
  restarts leaves no lock to clean up — the lease just stops being renewed.
- **A reaper.** Every worker periodically looks for `RUNNING` jobs whose lease has
  expired, closes out the orphaned `job_executions` row with "worker lease expired",
  and applies the normal retry policy. This is what recovers from a server restart
  during RUNNING and from a worker crashing mid-execution.
- **The attempt is recorded before the work starts.** `job_executions` gets its row and
  `attempt_count` is incremented *then* committed, before the task runs. If the process
  dies one line later, the attempt is still on disk and still counted, so a crash loop
  can't burn through retries invisibly.
- **Lease heartbeat.** While a task runs, a background thread pushes `lease_expires_at` forward on a third of the lease interval. Without it, any task slower than `LEASE_SECONDS` would be reclaimed mid-flight and executed a second time. It deliberately doesn't bump `version`, so the final compare-and-swap still works.
- **Graceful shutdown.** SIGTERM (what `docker stop` sends) stops the polling loop and
  drains in-flight jobs rather than killing them.

Delivery is therefore **at-least-once**, which is the honest guarantee for anything
that involves running side effects and surviving crashes. Exactly-once would need the
task itself to be transactional with the job row. The scheduler makes duplicates rare
(they need a genuine lease expiry) and the job payload carries a stable ID so handlers
can dedupe on their side.

## Retries

`max_retries` is retries *after* the first attempt, so `max_retries: 2` means up to
three attempts. Backoff is exponential — `5s, 10s, 20s, ...` capped at 5 minutes — and
the job goes back to `SCHEDULED` with `next_run_at` in the future between attempts,
rather than being held in memory. That way a retry survives the worker dying too.

For recurring jobs each occurrence gets a fresh retry budget on success.

When the budget is gone:

- one-off jobs → `FAILED`
- recurring jobs → `DEAD_LETTER`, so a permanently broken cron job stops firing but
  stays inspectable and can be put back with `POST /jobs/{id}/replay`

Set `DEAD_LETTER_ENABLED=false` if you'd rather everything just ended up in `FAILED`.

## Edge cases

| Case | Behaviour |
| --- | --- |
| `run_at` in the past | Rejected at creation with 422. A job already in the queue whose time has passed just runs late — it isn't dropped. |
| Server restart during RUNNING | Lease expires, reaper requeues it as a failed attempt. |
| Worker crash mid-execution | Same path; the open execution row is closed with the reason. |
| Max retries reached | `FAILED` / `DEAD_LETTER`, `next_run_at` cleared so nothing picks it up again. |
| Duplicate job creation | `idempotency_key` + unique index; the loser of a race reads back the winner's row. |
| Interval job after long downtime | Missed slots are skipped, not replayed — you get one run, not 60. |
| Two workers, one due job | Compare-and-swap claim; exactly one wins. |
| Stale worker returns from the dead | Its write is rejected by the version guard. |
| Invalid combinations (`interval_seconds` on a one-off job, cron on an interval job, bad cron string) | 422 with a specific message. |
| Task slower than the lease | A background heartbeat renews the lease every `LEASE_SECONDS/3` while the task runs, so a slow job isn't mistaken for a dead worker and run twice. |

## Configuration

Copy the template and edit it — the app loads `.env` automatically:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # put this in JWT_SECRET
```

`.env` is gitignored; `.env.example` is the committed template. Real environment
variables take precedence over the file, which is why docker-compose (which passes
them explicitly) is unaffected by whatever is in your local `.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | local Postgres | Compose passes the PostgreSQL connection string for the `db` service |
| `POLL_INTERVAL_SECONDS` | 2 | How often an idle worker looks for work |
| `BATCH_SIZE` | 10 | Max jobs claimed per poll |
| `WORKER_CONCURRENCY` | 4 | Threads per worker process |
| `LEASE_SECONDS` | 60 | Must be > your longest task |
| `RETRY_BACKOFF_SECONDS` / `_MAX_` | 5 / 300 | Exponential backoff base and cap |
| `FAILURE_RATE` | 0.3 | Simulated failure probability |
| `JWT_SECRET` | dev value | **Change it.** Must be shared by all API replicas |
| `ACCESS_TOKEN_MINUTES` | 60 | Token lifetime |
| `DEAD_LETTER_ENABLED` | true | Off → exhausted recurring jobs become FAILED |

## Tests

49 tests, ~15s, run against Postgres:

- `test_scheduling.py` — backoff curve, cron parsing, interval catch-up after downtime
- `test_api_jobs.py` — validation rules, filters, idempotency, pause/resume, 404/409 shapes
- `test_runner.py` — the full state machine: success, retry, exhaustion, rescheduling,
  lease recovery
- `test_concurrency.py` — two workers on one job, queue splitting, the stale-write race
- `test_auth.py` — register/login, forged and expired tokens, and the one that matters:
  two accounts cannot see or touch each other's jobs
- plus a regression test for the lease heartbeat, which caught a real duplicate-execution bug

Tasks are forced deterministic via the payload (`{"fail": true, "duration_seconds": 0}`)
so nothing in the suite sleeps or flakes.

## Things I'd do differently with more time

- **Alembic.** Schema is created with `create_all` on startup, which is fine for a demo
  and wrong for anything you'd deploy twice.
- **Notification instead of polling.** Postgres `LISTEN/NOTIFY` would cut the average
  latency between "due" and "running" from half a poll interval to ~0. Polling is
  simpler and doesn't lose jobs if a notification is missed, so it's the right starting
  point.
- **Metrics.** Queue depth, claim latency and attempt outcomes are the three numbers
  you'd actually page on. Structured logs are in place; Prometheus counters aren't.
- **Auth hardening.** Refresh tokens, revocation and per-user rate limiting. Today a
  stolen token is valid until it expires, which is the usual trade-off at this size.
