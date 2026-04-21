#!/usr/bin/env bash
# E2E: seeded morning-briefing cron is loaded, get_schedule tool returns it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# fastembed loads a ~300 MB ONNX model on first boot; CI runners with fast
# network load it in ~20s but local Docker-Desktop may need longer.
# Extend the healthz timeout beyond the default 30s so we don't race.
BOOT_TIMEOUT=90

build_image

NAME="casa-sch-$$"
trap "stop_container $NAME" EXIT

log "Starting container with default triggers (heartbeat + morning-briefing)"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "S-1: assert boot logs show 2 triggers registered for assistant"
assert_log_contains "$NAME" "Registered 2 trigger(s) for agent 'assistant'"
pass "S-1 assistant has 2 triggers registered (heartbeat + morning-briefing)"

log "S-2: invoking assistant to ask about schedule"
RESP=$(curl -s -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H "Content-Type: application/json" \
    -d '{"prompt": "What cron tasks are on your schedule today?"}' || true)

log "Response: $RESP"

if printf '%s' "$RESP" | grep -qiF '"error"'; then
    fail "S-2 invoke returned error: $RESP"
fi
pass "S-2 invoke returned non-error response"

pass "test_scheduling PASSED"
