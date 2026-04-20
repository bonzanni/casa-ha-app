#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# Repo root is two levels up from test-local/e2e/
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# Reuse the Windows-aware tmp base from test_migration.sh. Borrowing via
# copy rather than source-sharing keeps this script stand-alone.
_tmpbase() {
    local wintemp
    wintemp="$(powershell.exe -NoProfile -Command "[System.IO.Path]::GetTempPath()" 2>/dev/null | tr -d '\r\n')"
    if [ -n "$wintemp" ]; then
        local drive="${wintemp:0:1}"
        local rest="${wintemp:2}"
        rest="$(printf '%s' "$rest" | tr '\\' '/')"
        printf '/%s%s' "$(printf '%s' "$drive" | tr '[:upper:]' '[:lower:]')" "$rest"
    else
        printf '/tmp'
    fi
}
TMPBASE="$(_tmpbase)"

cleanup_all() {
    docker ps -q --filter "name=casa-deleg-.*-$$" | xargs -r docker stop >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

build_image

# ============================================================
# D-1: bundled-disabled contract (default env, no fixtures)
# ============================================================
log "D-1: bundled-disabled contract"

NAME="casa-deleg-d1-$$"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

# finance.yaml seeded to user config via the glob. MSYS_NO_PATHCONV=1
# prevents Git Bash from rewriting the container-side path to a Windows one.
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /addon_configs/casa-agent/agents/executors/finance.yaml \
    || fail "D-1: finance.yaml was not seeded to /addon_configs"

# Per-file disabled log line present.
assert_log_contains "$NAME" "Executor 'finance' bundled but disabled (file=finance.yaml)"

# Summary line: enabled=[] disabled=['finance'].
docker logs "$NAME" 2>&1 | grep -qE "Executors: enabled=\[\] disabled=\['finance'\]" \
    || { docker logs "$NAME" 2>&1 | grep -i executor | tail -10; fail "D-1: summary line missing or wrong shape"; }

# n8n not registered (no N8N_URL).
assert_log_not_contains "$NAME" "Registered n8n-workflows MCP server"

stop_container "$NAME"
pass "D-1 bundled-disabled contract"

# ============================================================
# D-2: flip-to-enabled contract (user-edited YAML via fixture mount)
# ============================================================
log "D-2: flip-to-enabled contract"

NAME="casa-deleg-d2-$$"
TMP_D2="${TMPBASE}/casa-deleg-d2-$$"
mkdir -p "$TMP_D2"
cp -r "${REPO_ROOT}/test-local/fixtures/delegation-enabled/." "$TMP_D2/"

# Capture fixture file hash to verify no migration clobbered it.
FIXTURE_HASH_BEFORE=$(sha256sum "$TMP_D2/agents/executors/finance.yaml" | awk '{print $1}')

MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -v "${TMP_D2}:/addon_configs/casa-agent" \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"

# 'loaded' line (executor registered for delegation dispatch).
docker logs "$NAME" 2>&1 | grep -qE "Executor 'finance' loaded \(model=" \
    || { docker logs "$NAME" 2>&1 | grep -i executor | tail -10; fail "D-2: 'loaded' line missing"; }

# Summary line reflects enabled finance.
docker logs "$NAME" 2>&1 | grep -qE "Executors: enabled=\['finance'\] disabled=\[\]" \
    || { docker logs "$NAME" 2>&1 | grep -i executor | tail -10; fail "D-2: summary line shows wrong state"; }

# No n8n registration (fixture has no N8N_URL).
assert_log_not_contains "$NAME" "Registered n8n-workflows MCP server"

stop_container "$NAME"

# Post-boot: the fixture's user-editable YAML must still be byte-identical
# to its pre-boot state (no migration overrode the user's enabled: true).
FIXTURE_HASH_AFTER=$(sha256sum "$TMP_D2/agents/executors/finance.yaml" | awk '{print $1}')
[ "$FIXTURE_HASH_BEFORE" = "$FIXTURE_HASH_AFTER" ] \
    || fail "D-2: fixture finance.yaml was modified by boot (hash drift)"

# Cleanup tmp (root-owned workspace/ files need containerized rm).
docker run --rm -v "${TMP_D2}:/target" --entrypoint sh "$IMAGE" \
    -c 'rm -rf /target/workspace /target/data' >/dev/null 2>&1 || true
rm -rf "$TMP_D2" 2>/dev/null || true

pass "D-2 flip-to-enabled contract"

# ============================================================
# D-3: second-executor discovery (config-not-code regression)
# ============================================================
log "D-3: second-executor discovery"

NAME="casa-deleg-d3-$$"
TMP_D3_DIR="${TMPBASE}/casa-deleg-d3-$$"
mkdir -p "$TMP_D3_DIR"
HEALTH_YAML="$TMP_D3_DIR/health.yaml"

# Minimal Tier 2 shape — enabled: false, empty channels, ephemeral session,
# zero token budget. Written inline; not committed as a fixture because the
# whole point is that *any* new YAML in defaults/ gets picked up.
cat > "$HEALTH_YAML" <<'YAML'
name: Doc
role: health
description: D-3 test fixture — do not ship.
enabled: false
model: sonnet
personality: |
  Test-only health executor stub.
tools:
  allowed: [Read]
  disallowed: [Bash, Write, Edit]
  permission_mode: acceptEdits
  max_turns: 5
mcp_server_names:
  - casa-framework
memory:
  token_budget: 0
session:
  strategy: ephemeral
  idle_timeout: 0
YAML

# Bind-mount the single file into the image's defaults executors dir. Docker
# supports file-level bind-mounts; setup-configs.sh's glob picks it up.
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -v "${HEALTH_YAML}:/opt/casa/defaults/agents/executors/health.yaml:ro" \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"

# Both files seeded to user config (the glob ran).
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /addon_configs/casa-agent/agents/executors/finance.yaml \
    || fail "D-3: finance.yaml was not seeded"
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /addon_configs/casa-agent/agents/executors/health.yaml \
    || fail "D-3: health.yaml was not seeded (glob failed)"

# Per-file disabled log line for health.
assert_log_contains "$NAME" "Executor 'health' bundled but disabled (file=health.yaml)"

# Summary line has both finance and health in disabled set (sorted alphabetically).
docker logs "$NAME" 2>&1 | grep -qE "Executors: enabled=\[\] disabled=\['finance', 'health'\]" \
    || { docker logs "$NAME" 2>&1 | grep -i executor | tail -10; fail "D-3: summary line missing or wrong shape"; }

stop_container "$NAME"
rm -rf "$TMP_D3_DIR" 2>/dev/null || true

pass "D-3 second-executor discovery"
