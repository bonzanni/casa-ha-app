#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-voice-sse-$$"
trap "stop_container $NAME" EXIT

build_image
log "Starting container $NAME"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "V-1: POST /api/converse streams SSE"
resp=$(curl -s -N -X POST \
    "http://localhost:${HOST_PORT}/api/converse" \
    -H 'content-type: application/json' \
    -d '{"prompt":"hi","agent_role":"butler","scope_id":"e2e-sse"}' \
    --max-time 20 || true)

if ! printf '%s\n' "$resp" | grep -q "event:"; then
    log "SSE response was:"
    printf '%s\n' "$resp" >&2
    fail "no SSE event frame returned"
fi

if printf '%s\n' "$resp" | grep -q "event: done"; then
    pass "SSE stream terminated with event: done"
elif printf '%s\n' "$resp" | grep -q "event: error"; then
    # Error frame is also an acceptable terminator — we're just smoke-testing
    # the pipeline, not the agent's ability to answer a real prompt.
    pass "SSE stream terminated with event: error (agent persona path exercised)"
else
    log "SSE response was:"
    printf '%s\n' "$resp" >&2
    fail "no SSE terminator (event: done or event: error)"
fi
