#!/usr/bin/env bash
# Finance use case end-to-end.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"

GEN=$(curl -s -X POST "$BASE_URL/generate" \
  -H 'content-type: application/json' \
  -d '{
    "use_case": "A mid-market bank wants to automate fraud triage across debit, credit, and ACH and accelerate SMB loan underwriting.",
    "company": "Meridian Bank"
  }')
echo "$GEN" | jq .
SESSION_ID=$(echo "$GEN" | jq -r .session_id)

curl -s -X POST "$BASE_URL/update" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\", \"action\": \"regenerate_agents\", \"instructions\": \"\"}" | jq .

curl -s -X POST "$BASE_URL/approve" \
  -H 'content-type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\"}" | jq .
