# SaaS Reliability & Support Operations

A small multi-tenant support-ticketing service ("Support Desk") used to practise production support. It's set up so it can be monitored, broken on purpose and fixed.

Results: 6 simulated incidents diagnosed and fixed (see [INCIDENTS.md](INCIDENTS.md)). Adding a missing index took the search query from 54 ms to 0.3 ms and the API request from 46 ms to 17 ms. The Bash health check runs all its checks in about 2 s, and all 30 tests pass.

## Services

Everything runs in Docker Compose:

| Service | Description | URL |
| --- | --- | --- |
| `api` | FastAPI REST service with roles (viewer, agent, admin) | http://localhost:8000/docs |
| `db` | PostgreSQL 16 | localhost:5432 |
| `mock-webhook` | Fake third-party notification service | internal |
| `postgres-exporter` | Database metrics for Prometheus | internal |
| `prometheus` | Collects metrics every 5 s | http://localhost:9090 |
| `grafana` | Service health dashboard | http://localhost:3000 |

## Requirements

- Docker Desktop (or Docker Engine with the Compose plugin)
- Bash and `curl` (Linux, macOS, or WSL on Windows)

## Getting started

```bash
chmod +x scripts/*.sh
docker compose up -d --build     # first run takes a few minutes to download images
docker compose ps                # check everything is running
scripts/seed.sh 1000000          # load 1 million fake tickets
scripts/healthcheck.sh           # run all the checks
```

To fill the dashboard with data, run some background traffic in a second terminal:

```bash
scripts/traffic.sh 600
```

To try the API at /docs, use the header `X-API-Key: agent-key-123`.

## Tests

```bash
docker compose run --rm tester
```

30 tests covering normal API use, bad input, the notification integration, and regressions. The regression tests fail if an incident comes back, for example `test_customer_search_within_budget` fails if the search index is dropped.

## API keys

| Key | Role | Org |
| --- | --- | --- |
| `admin-key-123` | admin | Acme |
| `agent-key-123` | agent | Acme |
| `viewer-key-123` | viewer | Acme |
| `globex-agent-key` | agent | Globex |
| `disabled-key-123` | agent (inactive) | Acme |

## Incidents

`scripts/scenario.sh` breaks and fixes the system one incident at a time:

```bash
scripts/scenario.sh list          # show all six
scripts/scenario.sh 3 break       # break it
scripts/scenario.sh 3 fix         # fix it
```

1. Slow query (missing index)
2. Authentication failure (deactivated account)
3. Integration failure (third-party timeout)
4. Bad deployment (wrong database password)
5. Data integrity (duplicates and future dates from a bad import)
6. Connection exhaustion (all database connections used up)

What happened in each one and how it was diagnosed and fixed is in [INCIDENTS.md](INCIDENTS.md).

Some useful commands for diagnosing:

```bash
docker compose logs api --since 5m | tail -20
docker compose logs api --since 5m | grep '"level": "ERROR"' | grep -o '"event": "[a-z_]*"' | sort | uniq -c
docker compose exec db psql -U app -d saas
```

## Scripts

- `scripts/healthcheck.sh`: checks containers, the API, the database, recent log errors, the integration and latency in one go
- `scripts/seed.sh`: loads fake tickets
- `scripts/measure.sh`: times the customer search endpoint
- `scripts/traffic.sh`: sends random traffic to the API

## Reset

```bash
docker compose down -v     # also deletes the database
```
