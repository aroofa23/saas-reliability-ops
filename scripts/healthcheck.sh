#!/usr/bin/env bash
# One-command diagnostics: containers, API, database, logs, integration, metrics
# Exits 0 if everything passes, 1 if anything fails
# Usage: scripts/healthcheck.sh
set -uo pipefail          # no -e: keep checking after a failure
cd "$(dirname "$0")/.."

API_URL="${API_URL:-http://localhost:8000}"
PROM_URL="${PROM_URL:-http://localhost:9090}"
ADMIN_KEY="admin-key-123"
failures=0
start=$(date +%s)

pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; failures=$((failures + 1)); }
psql_q() { docker compose exec -T db psql -U app -d saas -tAc "$1" 2>/dev/null; }

echo "== 1. Containers"
ps_out=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null)
for svc in db api mock-webhook postgres-exporter prometheus grafana; do
    state=$(awk -v s="$svc" '$1 == s {print $2}' <<< "$ps_out")
    if [[ "$state" == "running" ]]; then
        pass "$svc running"
    else
        fail "$svc is '${state:-not running}'"
    fi
done

echo "== 2. API"
read -r code secs < <(curl -s -o /tmp/hc_health.json -w '%{http_code} %{time_total}\n' \
                           --max-time 5 "$API_URL/health")
if [[ "$code" == "200" ]]; then
    pass "/health 200 in ${secs}s"
else
    fail "/health returned ${code:-000}: $(cat /tmp/hc_health.json 2>/dev/null)"
fi

echo "== 3. Database"
if docker compose exec -T db pg_isready -U app -d saas > /dev/null 2>&1; then
    pass "postgres accepting connections"
else
    fail "postgres not ready"
fi

conns=$(psql_q "SELECT count(*) FROM pg_stat_activity WHERE datname = 'saas'")
max=$(psql_q "SHOW max_connections")
if [[ "$conns" =~ ^[0-9]+$ && "$max" =~ ^[0-9]+$ ]]; then
    pct=$(( conns * 100 / max ))
    if (( pct >= 80 )); then
        fail "connections $conns/$max (${pct}%), near the limit"
        echo "       top holders:"
        psql_q "SELECT usename, state, left(query, 40), count(*) FROM pg_stat_activity
                WHERE datname = 'saas' GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 3" | sed 's/^/         /'
    else
        pass "connections $conns/$max"
    fi
else
    warn "could not read connection counts"
fi

idx=$(psql_q "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_tickets_customer_email'")
if [[ "$idx" == "1" ]]; then
    pass "index on tickets.customer_email present"
else
    warn "no index on tickets.customer_email (customer search will be slow)"
fi

uniq=$(psql_q "SELECT count(*) FROM pg_constraint WHERE conname = 'tickets_external_ref_key'")
if [[ "$uniq" == "1" ]]; then
    pass "unique constraint on external_ref present"
else
    dups=$(psql_q "SELECT count(*) FROM (SELECT external_ref FROM tickets GROUP BY 1 HAVING count(*) > 1) d")
    fail "unique constraint on external_ref missing; ${dups:-?} duplicated refs"
fi

future=$(psql_q "SELECT count(*) FROM tickets WHERE created_at > now() + interval '1 minute'")
if [[ "$future" == "0" ]]; then
    pass "no tickets dated in the future"
else
    fail "${future:-?} tickets dated in the future"
fi

echo "== 4. API logs, last 5 minutes"
logs=$(docker compose logs api --since 5m --no-log-prefix 2>/dev/null)
total=$(grep -c '"event": "request"' <<< "$logs")
errors_5xx=$(grep -c '"status": 5' <<< "$logs")
auth_401=$(grep -c '"status": 401' <<< "$logs")
echo "  requests: $total   5xx: $errors_5xx   401: $auth_401"
if (( total > 0 && errors_5xx * 100 / total >= 5 )); then
    fail "5xx rate is $(( errors_5xx * 100 / total ))%"
    echo "       most common error events:"
    grep '"level": "ERROR"' <<< "$logs" | grep -o '"event": "[a-z_]*"' | sort | uniq -c | sort -rn | head -3 \
        | sed 's/^/         /'
else
    pass "5xx rate under 5%"
fi
if (( auth_401 >= 10 )); then
    warn "$auth_401 auth failures; reasons and users:"
    grep '"event": "auth_rejected"' <<< "$logs" | grep -oE '"reason": "[^"]*"(, "user": "[^"]*")?' \
        | sort | uniq -c | sort -rn | head -3 | sed 's/^/         /'
fi

echo "== 5. Integration (notification service)"
first_id=$(psql_q "SELECT min(id) FROM tickets WHERE org_id = 1")
if [[ -n "$first_id" ]]; then
    read -r code secs < <(curl -s -o /dev/null -w '%{http_code} %{time_total}\n' --max-time 10 \
                               -X POST -H "X-API-Key: $ADMIN_KEY" "$API_URL/tickets/$first_id/notify")
    if [[ "$code" == "200" ]]; then
        pass "notify OK in ${secs}s"
    else
        fail "notify returned ${code:-000} after ${secs:-?}s"
    fi
else
    warn "no tickets to test notify with (run scripts/seed.sh)"
fi

echo "== 6. Metrics (Prometheus, last 5 minutes)"
p95=$(curl -s --max-time 5 "$PROM_URL/api/v1/query" \
        --data-urlencode 'query=histogram_quantile(0.95, sum by (le, path) (rate(http_request_duration_seconds_bucket[5m])))' \
      | grep -o '"path":"[^"]*"},"value":\[[^,]*,"[^"]*"' \
      | sed -E 's/"path":"([^"]*)"},"value":\[[^,]*,"([^"]*)"/\1 \2/')
if [[ -n "$p95" ]]; then
    echo "  p95 latency by route (s):"
    while read -r route value; do
        [[ "$value" == "NaN" ]] && continue
        printf '         %-28s %s\n' "$route" "$value"
    done <<< "$p95"
else
    warn "no latency data yet (is Prometheus up and has traffic run?)"
fi

echo
elapsed=$(( $(date +%s) - start ))
if (( failures == 0 )); then
    printf '\033[32mALL CHECKS PASSED\033[0m in %ss\n' "$elapsed"
    exit 0
else
    printf '\033[31m%s CHECK(S) FAILED\033[0m in %ss\n' "$failures" "$elapsed"
    exit 1
fi
