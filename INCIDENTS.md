# Incident log

Six production incidents simulated on this stack, each diagnosed and fixed. All numbers were measured locally.

## Environment

- MacBook (Apple M2), macOS, Docker Desktop
- 1,000,000 tickets seeded
- October 2026

## 1. Slow query

- **Symptom:** healthcheck.sh warned that tickets.customer_email had no index. measure.sh showed customer search averaging 46 ms (min 43, max 62), and Grafana showed p95 for /tickets/search at around 100 ms.
- **Cause:** `EXPLAIN ANALYZE` showed a Parallel Seq Scan. Postgres was checking about 1M rows to return 5, which took 54.0 ms, because customer_email had no index.
- **Fix:** Added the index: `CREATE INDEX idx_tickets_customer_email ON tickets (customer_email);`
- **Result:** The plan changed to an Index Scan and the query dropped to 0.32 ms (about 170x faster). measure.sh averaged 17 ms (min 12; first run 41 ms while warming up). Grafana p95 dropped to about 25 ms with no 5xx errors.
- **Prevention:** The regression test `test_customer_search_within_budget` fails if the index is dropped again. An alert on p95 latency would also catch it.
- **Next step:** Most of the remaining time comes from opening a new DB connection per request (twice: once for auth, once for the query). A connection pool would fix that.

## 2. Authentication failure

- **Symptom:** Requests with the agent's API key returned 401 "invalid or inactive API key".
- **Cause:** API logs showed auth_rejected with reason "inactive user" for agent@acme.test. The account had been deactivated. The key and the code were fine.
- **Fix:** Reactivated the account (`UPDATE users SET active = true`) after confirming it was deactivated by mistake. The same request returned tickets again.
- **Prevention:** Alert on a spike of 401s for a single user, and log who changes an account's status.

## 3. Integration failure

- **Symptom:** The notify endpoint returned 504 "notification service timed out" after 3.1 s.
- **Cause:** API logs showed integration_timeout for http://mock-webhook:9000/notify with the 3 s limit. The provider was taking 8 s to respond.
- **Fix:** Once the provider recovered, notify returned "notified": true in 59 ms (0.14 s total).
- **Prevention:** The 3 s timeout made the API fail fast instead of hanging. Retries with backoff, or a queue, would stop ticket updates from waiting on the provider.

## 4. Bad deployment

- **Symptom:** /health returned 503 with "database": "down", and healthcheck showed a 100% 5xx rate. The db container and all Postgres checks passed, so the database itself was fine.
- **Cause:** API logs showed "password authentication failed for user api_user". The new deployment had the wrong database password.
- **Fix:** Rolled back to the previous config (`docker compose up -d api`). /health returned "ok" and all checks passed.
- **Prevention:** A health check that blocks a bad rollout, and credentials kept in one place.

## 5. Data integrity

- **Symptom:** /admin/integrity reported 500 duplicate external_refs and 100 future-dated tickets, with "healthy": false.
- **Cause:** `GROUP BY external_ref HAVING count(*) > 1` showed refs like EXT-1 and EXT-10 appearing twice. The unique constraint on external_ref had been dropped, so a bad import could insert copies.
- **Fix:** Deleted the 500 copies (keeping the oldest row of each), reset the 100 future dates, and re-added the unique constraint. The integrity report went back to 0 / 0 / healthy.
- **Prevention:** Enforce constraints in the database, not just the app. The regression test for duplicate refs and validation in import scripts also help.

## 6. Connection exhaustion

- **Symptom:** /health returned 503 "database": "down". healthcheck showed connections at 38/40 (95%) and a 100% 5xx rate.
- **Cause:** healthcheck's "top holders" list (from pg_stat_activity) showed 36 api_user sessions stuck running pg_sleep and using up the slots. The admin could still log in because Postgres reserves a few slots for superusers.
- **Fix:** Terminated the 36 stuck sessions with `pg_terminate_backend`. Connections dropped to 2/40 and /health was ok again.
- **Prevention:** A connection pool in the API, idle session timeouts, and an alert at 80% of max_connections.
- **Note:** After the fix, the 5xx check in healthcheck kept failing for a few minutes, because it looks back over the last 5 minutes of logs.

## Diagnosis time

- Checking everything by hand (containers, health endpoint, DB connections, logs, integration) and reading the output takes several minutes.
- `scripts/healthcheck.sh` runs all the checks in about 2 s. The 31 "unknown key" 401s it reported were the deliberate bad-key requests from traffic.sh.

## Tests

- `docker compose run --rm tester`: 30 passed in 0.78 s (9 API, 13 negative, 3 integration, 5 regression)
