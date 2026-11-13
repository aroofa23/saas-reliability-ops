#!/usr/bin/env bash
# Generate realistic background traffic so the dashboards have something to show.
# Usage: scripts/traffic.sh [seconds]     (default 300; Ctrl-C to stop early)
set -uo pipefail

API_URL="${API_URL:-http://localhost:8000}"
duration="${1:-300}"
end=$(( $(date +%s) + duration ))
keys=(viewer-key-123 agent-key-123 admin-key-123)
count=0

echo "Sending traffic to $API_URL for ${duration}s..."
while (( $(date +%s) < end )); do
    key=${keys[$(( RANDOM % ${#keys[@]} ))]}
    case $(( RANDOM % 10 )) in
        0|1|2) curl -s -o /dev/null -H "X-API-Key: $key" "$API_URL/tickets?limit=20" ;;
        3|4|5) curl -s -o /dev/null -H "X-API-Key: $key" \
                   "$API_URL/tickets/search?customer_email=customer$(( RANDOM % 200000 ))@example.com" ;;
        6)     curl -s -o /dev/null -H "X-API-Key: $key" "$API_URL/tickets/$(( RANDOM % 1000 + 1 ))" ;;
        7)     curl -s -o /dev/null -X POST -H "X-API-Key: agent-key-123" \
                   "$API_URL/tickets/$(( (RANDOM % 900 + 1) ))/notify" ;;
        8)     curl -s -o /dev/null -X POST -H "X-API-Key: agent-key-123" -H "Content-Type: application/json" \
                   -d "{\"title\":\"Load test $count\",\"customer_email\":\"load$RANDOM@example.com\"}" \
                   "$API_URL/tickets" ;;
        9)     curl -s -o /dev/null -H "X-API-Key: bad-key" "$API_URL/tickets" ;;   # some expected 401s
    esac
    count=$(( count + 1 ))
    sleep 0.2
done
echo "Sent $count requests."
