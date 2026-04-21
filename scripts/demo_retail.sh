#!/usr/bin/env bash
# End-to-end retail flow: generate -> update -> approve.
# Requires `jq` for session_id extraction.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"

echo "== 1. generate =="
GEN=$(curl -s -X POST "$BASE_URL/generate" \
  -H 'content-type: application/json' \
  -d '{
    "use_case": "A 40-store apparel retailer wants AI support for replenishment and shrink detection at the store level.",
    "company": "Acme Apparel"
  }')
echo "$GEN" | jq .
SESSION_ID=$(echo "$GEN" | jq -r .session_id)

echo
echo "== 2. update (regenerate_personas) =="
curl -s -X POST "$BASE_URL/update" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\", \"action\": \"regenerate_personas\", \"instructions\": \"\"}" | jq .

echo
echo "== 3. update (modify) =="
curl -s -X POST "$BASE_URL/update" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\", \"action\": \"modify\", \"instructions\": \"Add a loss-prevention persona focused on organized retail crime.\"}" | jq .

echo
echo "== 4. approve =="
curl -s -X POST "$BASE_URL/approve" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\"}" | jq .

echo
echo "== 5. retrieve =="
curl -s "$BASE_URL/blueprint/$SESSION_ID" | jq .
