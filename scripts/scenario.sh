#!/usr/bin/env bash
# Break and fix the system on purpose, one incident at a time.
# Usage: scripts/scenario.sh <1-6> <break|fix>
#        scripts/scenario.sh list
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE=".scenario6.pids"

sql() {  # run SQL as the admin (superuser) role
    docker compose exec -T db psql -U app -d saas -v ON_ERROR_STOP=1 -c "$1"
}

usage() {
    cat <<'EOF'
usage: scripts/scenario.sh <1-6> <break|fix>
       scripts/scenario.sh list

  1  slow-query       ticket search does a full table scan (missing index)
  2  auth             a customer's API key is deactivated -> 401s
  3  integration      the notification service is slow -> timeouts (504)
  4  bad-deploy       API redeployed with the wrong DB password -> 503s
  5  data-integrity   a bad import created duplicates and future dates
  6  connections      something holds every DB connection -> 503s
EOF
}

action="${2:-}"
case "${1:-}" in
    list|"")
        usage
        exit 0
        ;;
esac
if [[ "$action" != "break" && "$action" != "fix" ]]; then
    usage >&2
    exit 1
fi

case "$1:$action" in
    1:break)
        sql "DROP INDEX IF EXISTS idx_tickets_customer_email;"
        ;;
    1:fix)
        # CONCURRENTLY builds the index without locking writes to the table
        sql "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tickets_customer_email ON tickets (customer_email);"
        sql "ANALYZE tickets;"
        ;;

    2:break)
        sql "UPDATE users SET active = false WHERE email = 'agent@acme.test';"
        ;;
    2:fix)
        sql "UPDATE users SET active = true WHERE email = 'agent@acme.test';"
        ;;

    3:break)
        MOCK_DELAY=8 docker compose up -d mock-webhook
        ;;
    3:fix)
        docker compose up -d mock-webhook
        ;;

    4:break)
        API_DB_PASSWORD=wrong-password docker compose up -d api
        ;;
    4:fix)
        docker compose up -d api
        ;;

    5:break)
        sql "ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_external_ref_key;"
        sql "INSERT INTO tickets (org_id, external_ref, title, body, customer_email, status, priority, created_at)
             SELECT org_id, external_ref, title, body, customer_email, status, priority, created_at
             FROM tickets WHERE id <= 500;"
        sql "UPDATE tickets SET created_at = now() + interval '30 days' WHERE id BETWEEN 1000 AND 1099;"
        ;;
    5:fix)
        # keep the oldest row for each external_ref, delete the copies
        sql "DELETE FROM tickets a USING tickets b
             WHERE a.external_ref = b.external_ref AND a.id > b.id;"
        sql "UPDATE tickets SET created_at = now() WHERE created_at > now() + interval '1 minute';"
        sql "ALTER TABLE tickets ADD CONSTRAINT tickets_external_ref_key UNIQUE (external_ref);"
        ;;

    6:break)
        # Open 40 idle sessions as the API's own role; they hog every normal slot.
        : > "$PID_FILE"
        for i in $(seq 1 40); do
            docker compose exec -T db \
                psql "postgresql://api_user:apisecret@localhost/saas" -c "SELECT pg_sleep(900)" \
                >/dev/null 2>&1 &
            echo $! >> "$PID_FILE"
        done
        sleep 3
        echo "Started $(wc -l < "$PID_FILE") hogging sessions (PIDs in $PID_FILE)."
        ;;
    6:fix)
        # The superuser can still log in: Postgres reserves a few slots for it.
        sql "SELECT count(pg_terminate_backend(pid)) AS terminated FROM pg_stat_activity
             WHERE usename = 'api_user' AND query LIKE 'SELECT pg_sleep%';"
        if [[ -f "$PID_FILE" ]]; then
            xargs kill 2>/dev/null < "$PID_FILE" || true
            rm -f "$PID_FILE"
        fi
        ;;

    *)
        usage >&2
        exit 1
        ;;
esac

echo "Scenario $1: $action done."
