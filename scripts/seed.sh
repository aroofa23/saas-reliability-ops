#!/usr/bin/env bash
# Load the tickets table with fake data so slow queries become visible.
# Usage: scripts/seed.sh [rows]        (default 3,000,000)
set -euo pipefail
cd "$(dirname "$0")/.."

rows="${1:-3000000}"
if ! [[ "$rows" =~ ^[0-9]+$ ]]; then
    echo "usage: $0 [rows]   (rows must be a whole number)" >&2
    exit 1
fi

echo "Seeding $rows tickets (this can take a minute or two)..."
start=$(date +%s)

docker compose exec -T db psql -U app -d saas -v ON_ERROR_STOP=1 -v rows="$rows" <<'SQL'
TRUNCATE tickets RESTART IDENTITY;
INSERT INTO tickets (org_id, external_ref, title, body, customer_email, status, priority, created_at)
SELECT
    CASE WHEN i % 10 = 0 THEN 2 ELSE 1 END,                      -- 90% Acme, 10% Globex
    'EXT-' || i,
    'Ticket ' || i,
    repeat('Customer reported an issue. ', 6),
    'customer' || (i % 200000) || '@example.com',               -- ~15 tickets per customer
    (ARRAY['open','pending','resolved','closed'])[1 + i % 4],
    (ARRAY['low','normal','high','urgent'])[1 + i % 4],
    now() - (i % 365) * interval '1 day' - (i % 86400) * interval '1 second'
FROM generate_series(1, :rows) AS i;
ANALYZE tickets;
SELECT count(*) AS tickets, pg_size_pretty(pg_total_relation_size('tickets')) AS table_size FROM tickets;
SQL

echo "Done in $(( $(date +%s) - start ))s."
