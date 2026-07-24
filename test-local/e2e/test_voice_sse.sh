#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# #193 (v0.117.0): the voice routes are fail-CLOSED — no secret means every
# voice route is OFF. This test runs two containers in SEQUENCE (they share
# HOST_PORT, so never concurrently):
#   1. the default no-secret fixture — every voice route must refuse, and
#   2. an auth-ON fixture — a correctly SIGNED turn must still stream.
NAME="casa-voice-sse-$$"
AUTH_NAME="casa-voice-sse-auth-$$"
trap 'stop_container $NAME; stop_container $AUTH_NAME' EXIT

build_image

# ---------------------------------------------------------------- no secret
log "Starting no-secret container $NAME"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "V-1: POST /api/converse fails closed without a secret"
if MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    'test ! -s /data/webhook_secret'; then
    pass "no-secret fixture has no non-empty generated or configured secret"
else
    fail "no-secret fixture unexpectedly has a non-empty webhook secret"
fi

turn_response=$(curl -sS -w $'\n%{http_code}' -X POST \
    "http://localhost:${HOST_PORT}/api/converse" \
    -H 'content-type: application/json' \
    -d '{"prompt":"hi","agent_role":"butler","scope_id":"e2e-sse"}' \
    --max-time 10 || true)
turn_status=${turn_response##*$'\n'}

if [ "$turn_status" != "401" ]; then
    log "voice turn response was:"
    printf '%s\n' "$turn_response" >&2
    fail "unsigned voice turn without a secret returned HTTP $turn_status, expected 401"
fi
pass "unsigned voice turn rejected with 401 (butler never reached)"

log "V-2: GET /api/voice/agents fails closed without a secret"
catalog_response=$(curl -sS -w $'\n%{http_code}' \
    "http://localhost:${HOST_PORT}/api/voice/agents" \
    --max-time 5 || true)
catalog_status=${catalog_response##*$'\n'}
catalog_body=${catalog_response%$'\n'*}

if [ "$catalog_status" != "401" ]; then
    fail "voice catalog without a secret returned HTTP $catalog_status, expected 401"
fi

if [ "$catalog_body" != '{"error": "invalid signature"}' ]; then
    fail "voice catalog without a secret did not return the generic error body"
fi

pass "voice catalog rejects no-secret discovery with a generic 401"

stop_container "$NAME"

# ----------------------------------------------------------------- auth on
log "Starting auth-ON container $AUTH_NAME"
start_authed_container "$AUTH_NAME" >/dev/null
wait_healthy "$AUTH_NAME"

log "V-3: a SIGNED POST /api/converse streams SSE"
body='{"prompt":"hi","agent_role":"butler","scope_id":"e2e-sse"}'
resp=$(curl -s -N -X POST \
    "http://localhost:${HOST_PORT}/api/converse" \
    -H 'content-type: application/json' \
    -H "X-Webhook-Signature: $(sign_body "$body")" \
    -d "$body" \
    --max-time 20 || true)

if ! printf '%s\n' "$resp" | grep -q "event:"; then
    log "SSE response was:"
    printf '%s\n' "$resp" >&2
    fail "no SSE event frame returned for a signed turn"
fi

if printf '%s\n' "$resp" | grep -q "event: done"; then
    pass "signed SSE stream terminated with event: done"
elif printf '%s\n' "$resp" | grep -q "event: error"; then
    # Error frame is also an acceptable terminator — we're just smoke-testing
    # the pipeline, not the agent's ability to answer a real prompt.
    pass "signed SSE stream terminated with event: error (agent persona path exercised)"
else
    log "SSE response was:"
    printf '%s\n' "$resp" >&2
    fail "no SSE terminator (event: done or event: error)"
fi
