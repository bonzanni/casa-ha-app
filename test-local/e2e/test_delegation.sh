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

# finance/ dir seeded to user config via the seed_agent_dir helper.
# MSYS_NO_PATHCONV=1 prevents Git Bash from rewriting the container-side
# path to a Windows one.
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /addon_configs/casa-agent/agents/executors/finance/runtime.yaml \
    || fail "D-1: finance/ was not seeded to /addon_configs"

# Per-executor disabled log line present (post-cut shape — no file=...).
assert_log_contains "$NAME" "Executor 'finance' bundled but disabled"

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

# Capture fixture directory hash (sha256 over every file's content) to verify
# no boot-time side-effect mutated the user's YAMLs. Finds under a subshell
# so the cwd change doesn't leak.
_dir_hash() {
    ( cd "$1" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}' )
}
FIXTURE_HASH_BEFORE=$(_dir_hash "$TMP_D2/agents/executors/finance")

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

# Post-boot: the fixture's user-editable directory must still be content-
# identical to its pre-boot state (no migration overrode the user's
# enabled: true). Hash folds every file's sha256 into one.
FIXTURE_HASH_AFTER=$(_dir_hash "$TMP_D2/agents/executors/finance")
[ "$FIXTURE_HASH_BEFORE" = "$FIXTURE_HASH_AFTER" ] \
    || fail "D-2: fixture finance/ dir was modified by boot (hash drift)"

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
HEALTH_DIR="$TMP_D3_DIR/health"
mkdir -p "$HEALTH_DIR"

# Minimal Tier 2 directory — enabled: false, empty channels, ephemeral
# session, zero token budget. Written inline; not committed as a fixture
# because the whole point is that *any* new executor dir in defaults/
# gets picked up by the seed_agent_dir glob.
cat > "$HEALTH_DIR/character.yaml" <<'YAML'
schema_version: 1
name: Doc
role: health
archetype: health-executor
card: |
  D-3 test fixture — do not ship.
prompt: |
  Test-only health executor stub.
YAML
printf 'schema_version: 1\n' > "$HEALTH_DIR/voice.yaml"
printf 'schema_version: 1\n' > "$HEALTH_DIR/response_shape.yaml"
cat > "$HEALTH_DIR/runtime.yaml" <<'YAML'
schema_version: 1
enabled: false
model: sonnet
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

# Bind-mount the directory into the image's defaults executors dir. Docker
# supports directory bind-mounts; setup-configs.sh's seed_agent_dir picks
# it up on first boot.
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -v "${HEALTH_DIR}:/opt/casa/defaults/agents/executors/health:ro" \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"

# Both dirs seeded to user config (the seed_agent_dir ran for each).
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /addon_configs/casa-agent/agents/executors/finance/runtime.yaml \
    || fail "D-3: finance/ was not seeded"
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /addon_configs/casa-agent/agents/executors/health/runtime.yaml \
    || fail "D-3: health/ was not seeded"

# Per-executor disabled log line for health (post-cut shape — no file=...).
assert_log_contains "$NAME" "Executor 'health' bundled but disabled"

# Summary line has both finance and health in disabled set (sorted alphabetically).
docker logs "$NAME" 2>&1 | grep -qE "Executors: enabled=\[\] disabled=\['finance', 'health'\]" \
    || { docker logs "$NAME" 2>&1 | grep -i executor | tail -10; fail "D-3: summary line missing or wrong shape"; }

stop_container "$NAME"
rm -rf "$TMP_D3_DIR" 2>/dev/null || true

pass "D-3 second-executor discovery"
