#!/usr/bin/env bash
# Healthcare use case end-to-end.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"

GEN=$(curl -s -X POST "$BASE_URL/generate" \
  -H 'content-type: application/json' \
  -d '{
    "use_case": "Multi-specialty clinic wants to reduce insurance pre-authorization delays and streamline referral scheduling."
  }')
echo "$GEN" | jq .
SESSION_ID=$(echo "$GEN" | jq -r .session_id)

curl -s -X POST "$BASE_URL/update" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\", \"action\": \"modify\", \"instructions\": \"Add a scenario covering prescription refill reauthorization for specialty medications.\"}" | jq .

curl -s -X POST "$BASE_URL/approve" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\"}" | jq .
