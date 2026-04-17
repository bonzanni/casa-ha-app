#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# On Windows with Git Bash + Docker Desktop (WSL2 backend), /tmp paths from
# mktemp -d are MSYS-local and invisible to Docker.  Use a path under the
# user's Windows home (/c/Users/...) which Docker Desktop shares by default.
# Also set MSYS_NO_PATHCONV=1 so docker.exe receives the path verbatim.
_tmpbase() {
    # Try to derive a Docker-visible temp dir.
    # /c/Users/.../AppData/Local/Temp is always shared with Docker Desktop.
    local wintemp
    wintemp="$(powershell.exe -NoProfile -Command "[System.IO.Path]::GetTempPath()" 2>/dev/null | tr -d '\r\n')"
    if [ -n "$wintemp" ]; then
        # Convert C:\...\  →  /c/.../
        local drive="${wintemp:0:1}"
        local rest="${wintemp:2}"
        rest="$(printf '%s' "$rest" | tr '\\' '/')"
        printf '/%s%s' "$(printf '%s' "$drive" | tr '[:upper:]' '[:lower:]')" "$rest"
    else
        # Non-Windows fallback
        printf '/tmp'
    fi
}

TMPBASE="$(_tmpbase)"

# Repo root is two levels up from test-local/e2e/
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

build_image

NAME="casa-hb-$$"
trap "stop_container $NAME" EXIT

log "Starting container with heartbeat_interval=1, heartbeat_enabled=true"
# Override options.json via volume mount read by 03-export-env.sh.
# Also provide a full addon_configs/casa-agent directory with a custom
# schedules.yaml (interval_minutes=1) to override the image default (60 min).
tmp="${TMPBASE}/casa-hb-$$"
mkdir -p "$tmp/data"
mkdir -p "$tmp/config/agents"

# python3 on Windows is native and needs Windows-style paths.
# cygpath -w converts /c/Users/... → C:\Users\...
WIN_SRC="$(cygpath -w "${REPO_ROOT}/test-local/options.json.example")"
WIN_DST="$(cygpath -w "${tmp}/data/options.json")"
python3 - "$WIN_SRC" "$WIN_DST" <<'PYEOF'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    d = json.load(f)
d["heartbeat_enabled"] = True
d["heartbeat_interval_minutes"] = 1
with open(dst, "w") as f:
    json.dump(d, f, indent=2)
PYEOF

# Copy default agent/webhook configs so init-setup-configs finds them and
# skips recreation.  Then write a custom schedules.yaml with interval=1 so
# the scheduler loop fires within the 70 s test window.
DEFAULTS="${REPO_ROOT}/casa-agent/rootfs/opt/casa/defaults"
cp -r "${DEFAULTS}/agents/." "${tmp}/config/agents/"
cp "${DEFAULTS}/webhooks.yaml" "${tmp}/config/webhooks.yaml"
cat > "${tmp}/config/schedules.yaml" <<'SCHEDEOF'
tasks: []

heartbeat:
  enabled: true
  interval_minutes: 1
  agent: assistant
  channel: scheduler
  prompt: "Heartbeat: check for pending tasks."
SCHEDEOF

MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -v "${tmp}/data/options.json:/data/options.json" \
    -v "${tmp}/config:/addon_configs/casa-agent" \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"

log "Waiting 70s for the first heartbeat tick..."
sleep 70

log "C-1: heartbeat fired and reached agent loop"
assert_log_contains "$NAME" "Heartbeat firing for agent 'assistant'"
# The mock SDK logs a client_init on every agent call. A working heartbeat
# produces exactly one entry per tick.
count=$(docker exec "$NAME" \
    sh -c 'grep -c "\"event\": \"client_init\"" /data/mock_sdk_calls.jsonl || true')
[ "$count" -ge 1 ] \
    || fail "heartbeat tick did not invoke the SDK (client_init count=$count)"
pass "C-1 heartbeat delivered (client_init count=$count)"

assert_log_not_contains "$NAME" "channel is required"
assert_log_not_contains "$NAME" "ValueError"
pass "no ValueError('channel is required') (BUG-H1 regression)"
