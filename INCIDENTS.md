# Incident log

Six production incidents I simulated, diagnosed and fixed on this stack, with the symptom, root cause, fix and prevention for each. All numbers were measured on my machine.

## Environment

- Machine / OS: MacBook (Apple M2), macOS, Docker Desktop
- Tickets seeded: 1,000,000
- Date built: November 2026

## 1. Slow query

- What I saw first (dashboard / healthcheck): healthcheck.sh warned there was no index on tickets.customer_email; measure.sh showed customer search averaging 46 ms; Grafana p95 for /tickets/search ~100 ms
- `EXPLAIN ANALYZE` before (plan type + execution time): Parallel Seq Scan, ~1M rows checked to return 5, 54.0 ms
- `scripts/measure.sh` before (avg): 46 ms (min 43, max 62)
- Fix: CREATE INDEX idx_tickets_customer_email ON tickets (customer_email);
- `EXPLAIN ANALYZE` after: Index Scan using idx_tickets_customer_email, 0.32 ms (~170x faster)
- `scripts/measure.sh` after (avg): 17 ms (min 12; first run 41 ms while warming up)
- Grafana after: p95 for /tickets/search ~25 ms, no 5xx errors
- Remaining time: mostly opening a new DB connection per request (twice: auth + query). Next fix: a connection pool.
- One sentence I'd say in an interview: EXPLAIN ANALYZE showed Postgres scanning all million rows to return five because customer_email had no index; adding one took the query from 54 ms to 0.3 ms and the API request from 46 ms to about 17 ms.

## 2. Authentication failure
- What I saw first: requests with the agent's API key returned 401 "invalid or inactive API key"
- How I found the cause: API logs showed auth_rejected with reason "inactive user" for agent@acme.test, so the account had been deactivated; the key and the code were fine
- Fix: reactivated the account (UPDATE users SET active = true) after confirming it was deactivated by mistake; the same request returned tickets again
- Prevention: alert on a spike of 401s for a single user; log who changes an account's status

## 3. Integration failure
- What I saw first: notify returned 504 "notification service timed out" after 3.1 s (time curl)
- How I found the cause: API logs showed integration_timeout for http://mock-webhook:9000/notify with the 3 s limit; the provider was taking 8 s to respond
- Fix: provider restored; notify back to "notified": true in 59 ms (0.14 s total)
- Prevention: the 3 s timeout made it fail fast instead of hanging; add retries with backoff, or a queue so ticket updates don't wait on the provider

## 4. Bad deployment
- What I saw first: /health returned 503 with "database": "down"; healthcheck showed API FAIL and a 100% 5xx rate, but the db container and all Postgres checks PASSED
- How I found the cause: API logs showed "password authentication failed for user api_user", so the new deployment had the wrong database password
- Fix: rolled back to the previous config (docker compose up -d api); /health back to "ok" and all checks passed
- Prevention: a health check that blocks a bad rollout; credentials managed in one place

## 5. Data integrity
- What I saw first: /admin/integrity reported 500 duplicate external_refs and 100 future-dated tickets, "healthy": false
- How I found the cause: SQL GROUP BY external_ref HAVING count(*) > 1 showed refs like EXT-1, EXT-10 appearing twice; the unique constraint on external_ref had been dropped, so a bad import could insert copies
- Fix: deleted the 500 copies keeping the oldest row, reset the 100 future dates, re-added the unique constraint; integrity report back to 0 / 0 / healthy
- Prevention: constraints enforced in the database (not just the app), the regression test for duplicate refs, validation in import scripts

## 6. Connection exhaustion
- What I saw first: /health returned 503 "database": "down"; healthcheck FAIL: connections 38/40 (95%) with 36 api_user sessions stuck in pg_sleep, 5xx rate 100%
- How I found the cause: healthcheck's "top holders" list (from pg_stat_activity) showed 36 api_user sessions stuck running pg_sleep and using up the slots; the admin could still log in because Postgres reserves slots for superusers
- Fix: terminated the 36 stuck sessions (pg_terminate_backend); connections back to 2/40, /health ok
- Prevention: connection pool in the API, idle session timeouts, alert at 80% of max_connections
- Note: after the fix the log-based 5xx check still failed for a few minutes because it looks back over the last 5 minutes

## Diagnosis time

- Checking everything by hand: running each check separately (containers, health endpoint, DB connections, logs, integration) and reading the output takes several minutes
- `scripts/healthcheck.sh`: about 2 s for all checks (containers, API, DB connections, index and constraint, data dates, log error rates, integration, p95 latency). All passed. The 31 "unknown key" 401s were the deliberate bad-key requests from traffic.sh.

## Tests
- `docker compose run --rm tester` result: 30 passed in 0.78 s (9 API, 13 negative, 3 integration, 5 regression)
