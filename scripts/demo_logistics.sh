#!/usr/bin/env bash
# Logistics use case end-to-end.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"

GEN=$(curl -s -X POST "$BASE_URL/generate" \
  -H 'content-type: application/json' \
  -d '{
    "use_case": "Regional LTL carrier needs AI for day-of routing exceptions, dock orchestration, and driver check-in monitoring.",
    "company": "Northern Freight"
  }')
echo "$GEN" | jq .
SESSION_ID=$(echo "$GEN" | jq -r .session_id)

curl -s -X POST "$BASE_URL/update" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\", \"action\": \"regenerate_scenarios\", \"instructions\": \"\"}" | jq .

curl -s -X POST "$BASE_URL/approve" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\"}" | jq .
