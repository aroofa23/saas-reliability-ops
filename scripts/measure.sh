#!/usr/bin/env bash
# Time the customer search endpoint several times and print min/avg/max.
# Usage: scripts/measure.sh [customer_email] [runs]
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
email="${1:-customer4242@example.com}"
runs="${2:-10}"

echo "Timing GET /tickets/search?customer_email=$email ($runs runs)"
times=()
for i in $(seq 1 "$runs"); do
    t=$(curl -s -o /dev/null -w '%{time_total}' -H "X-API-Key: viewer-key-123" \
          "$API_URL/tickets/search?customer_email=$email")
    printf '  run %2d: %ss\n' "$i" "$t"
    times+=("$t")
done

printf '%s\n' "${times[@]}" | awk '
    NR == 1 { min = $1; max = $1 }
    { sum += $1; if ($1 < min) min = $1; if ($1 > max) max = $1 }
    END { printf "min %.3fs   avg %.3fs   max %.3fs\n", min, sum / NR, max }'
