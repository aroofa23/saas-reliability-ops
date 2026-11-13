# Support Desk SaaS: Reliability Lab

A small multi-tenant support-ticketing service that you run, monitor, break on purpose and fix. Everything runs in Docker Compose:

| Service | What it is | URL |
| --- | --- | --- |
| `api` | FastAPI REST service with role-based access (viewer < agent < admin) | http://localhost:8000/docs |
| `db` | PostgreSQL 16 | localhost:5432 |
| `mock-webhook` | Fake third-party notification service | (internal) |
| `postgres-exporter` | Exposes database metrics to Prometheus | (internal) |
| `prometheus` | Collects metrics every 5 s | http://localhost:9090 |
| `grafana` | "Support Desk SaaS - Service Health" dashboard | http://localhost:3000 |

## You need

- Docker Desktop (or Docker Engine + the Compose plugin): `docker compose version` should work.
- A Bash terminal with `curl`: Linux, macOS, or WSL on Windows.
- Postman (optional) for manual API calls.

## Part 1 (Wednesday evening, about 3.5 hours)

```bash
chmod +x scripts/*.sh
docker compose up -d --build          # first run downloads images: allow ~5-10 min
docker compose ps                     # everything should say "running"
curl -s localhost:8000/health         # {"status": "ok", ...}
scripts/seed.sh                       # 3,000,000 tickets; use a smaller number on a slow laptop
scripts/healthcheck.sh                # your diagnostics script
```

Open http://localhost:8000/docs and try the endpoints with the header `X-API-Key: agent-key-123`. Then run background traffic in a second terminal so the dashboard fills up:

```bash
scripts/traffic.sh 600
```

Then do **scenario 1** (below) and record your before and after numbers in `INCIDENTS.md`.

## Part 2 (Thursday 9-12)

Scenarios 2 to 6, then the test suite:

```bash
docker compose run --rm tester        # 30 tests: API, negative, integration, regression
```

Try a test failing on purpose: run `scripts/scenario.sh 1 break`, run the tests, watch `test_customer_search_within_budget` fail, then fix it again. That's what a regression test is for.

## API keys

| Key | User | Role | Org |
| --- | --- | --- | --- |
| `admin-key-123` | admin@acme.test | admin | Acme |
| `agent-key-123` | agent@acme.test | agent | Acme |
| `viewer-key-123` | viewer@acme.test | viewer | Acme |
| `globex-agent-key` | agent@globex.test | agent | Globex |
| `disabled-key-123` | disabled@acme.test | agent (inactive) | Acme |

## The six incidents

Run `scripts/scenario.sh <n> break`, then **diagnose it yourself before reading the answer**: dashboard first, then `scripts/healthcheck.sh`, then logs and SQL. Fix with `scripts/scenario.sh <n> fix`, or better, with the commands by hand.

Useful diagnosis commands:

```bash
docker compose logs api --since 5m | tail -20
docker compose logs api --since 5m | grep '"level": "ERROR"' | grep -o '"event": "[a-z_]*"' | sort | uniq -c
docker compose exec db psql -U app -d saas          # SQL shell as the admin
```

### 1. Slow query (database bottleneck)
- **Symptom:** p95 latency for `/tickets/search` jumps on Grafana; users say search is slow.
- **Measure:** `scripts/measure.sh` (before), then again after the fix.
- **Diagnose:** in psql: `EXPLAIN ANALYZE SELECT * FROM tickets WHERE customer_email = 'customer4242@example.com' AND org_id = 1 ORDER BY created_at DESC LIMIT 50;` and look for `Seq Scan on tickets` and the execution time.
- **Fix:** `CREATE INDEX CONCURRENTLY idx_tickets_customer_email ON tickets (customer_email);` then run `EXPLAIN ANALYZE` again: it should now say `Index Scan` or `Bitmap Index Scan`.
- **Prevent:** the regression test `test_customer_search_within_budget`; a Grafana alert on p95 latency.
- Note: the index doesn't exist at the start, so scenario 1 is already "broken" after seeding. `scenario.sh 1 break` drops it again later.

### 2. Authentication failure
- **Symptom:** a spike of 401s on the "4xx responses" panel; one customer says everything stopped working.
- **Diagnose:** `healthcheck.sh` lists auth failure reasons and users; logs show `auth_rejected` with `"reason": "inactive user"`; in SQL, `SELECT email, role, active FROM users;`
- **Fix:** reactivate the account (after confirming it was deactivated by mistake).
- **Prevent:** alert on a 401 spike for one user; log who changed the account.

### 3. Integration failure (third-party timeout)
- **Symptom:** `/tickets/{id}/notify` returns 504 after about 3 s; the "Notification service failures" panel rises.
- **Diagnose:** logs show `integration_timeout` with the URL and the 3 s timeout; `docker compose logs mock-webhook` shows `delay=8.0s`.
- **Fix:** the provider recovers (`scenario.sh 3 fix`). On our side the API failed fast with a clear 504 instead of hanging, which is the important part.
- **Prevent:** timeouts on every outbound call (already there), retries with backoff, or a queue so ticket updates don't wait on the provider.

### 4. Bad deployment (wrong configuration)
- **Symptom:** every request returns 503; `/health` says `"database": "down"`, but the database container is healthy.
- **Diagnose:** logs show `db_connect_failed` with `password authentication failed for user "api_user"`; `docker compose exec api env | grep DATABASE_URL` shows the bad value.
- **Fix:** redeploy with the correct configuration (`docker compose up -d api`), i.e. roll back.
- **Prevent:** a health check that blocks the rollout; secrets kept in one managed place.

### 5. Data integrity (bad import)
- **Symptom:** `GET /admin/integrity` (admin key) reports duplicates and future-dated tickets; customers see the same ticket twice.
- **Diagnose:** `SELECT external_ref, count(*) FROM tickets GROUP BY 1 HAVING count(*) > 1 LIMIT 10;` and `SELECT count(*) FROM tickets WHERE created_at > now();` and `\d tickets` shows the unique constraint is gone.
- **Fix:** delete copies keeping the oldest row, correct the dates, re-add the unique constraint (see `scenario.sh`).
- **Prevent:** constraints in the database (not just in the app), the regression test `test_duplicate_external_ref_rejected`, validation in import scripts.

### 6. Connection exhaustion
- **Symptom:** intermittent then constant 503s; "Database connections in use" flat at the limit (40).
- **Diagnose:** `healthcheck.sh` shows connections near the limit and the top holders; in SQL: `SELECT usename, state, query, count(*) FROM pg_stat_activity GROUP BY 1,2,3 ORDER BY 4 DESC;` shows many idle `pg_sleep` sessions.
- **Fix:** terminate the stuck sessions (`pg_terminate_backend`). The admin can still log in because Postgres reserves slots for superusers.
- **Prevent:** a connection pool in the API, `idle_in_transaction_session_timeout`, an alert at 80% of `max_connections`.

## Read the Bash scripts line by line

These scripts are your Bash practice for the interview. Read each one until you can explain every line:

| Script | Bash ideas in it |
| --- | --- |
| `scripts/seed.sh` | `set -euo pipefail`, default arguments `${1:-…}`, regex test `=~`, heredocs, `$(date +%s)` arithmetic |
| `scripts/measure.sh` | `for` + `seq`, arrays, `curl -w`, piping into `awk` for min/avg/max |
| `scripts/scenario.sh` | functions, `case` with patterns, background jobs `&` and `$!`, PID files, `xargs kill` |
| `scripts/healthcheck.sh` | why no `-e`, `read` from process substitution `< <(...)`, here-strings `<<<`, `grep -c`, `grep -o \| sort \| uniq -c \| sort -rn`, `(( ))` arithmetic, exit codes |
| `scripts/traffic.sh` | `$RANDOM`, arrays and `${#array[@]}`, `while` with a time limit |

## Reset everything

```bash
docker compose down -v      # -v also deletes the database volume
```
