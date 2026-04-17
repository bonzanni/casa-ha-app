#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-smoke-$$"
trap "stop_container $NAME" EXIT

build_image

log "Starting container $NAME"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "A-2: /healthz returns 200"
curl -sf "http://localhost:${HOST_PORT}/healthz" >/dev/null \
  || fail "/healthz did not return 200"
pass "/healthz OK"

log "A-3: dashboard / returns 200 (BUG-D1 regression)"
# Hit / repeatedly during the window after HTTP start; if heartbeat defaults
# are not initialised before the dashboard closure is defined, this
# UnboundLocalErrors. Fixed in v0.2.0 by init_heartbeat_defaults().
for i in 1 2 3 4 5; do
    curl -sf "http://localhost:${HOST_PORT}/" >/dev/null \
        || fail "dashboard / returned non-200 on attempt $i"
done
pass "dashboard / OK (5/5 requests)"

assert_log_not_contains "$NAME" "UnboundLocalError"
assert_log_not_contains "$NAME" "No agent with role 'assistant' found"
pass "no UnboundLocalError, no missing-agent error in logs"
