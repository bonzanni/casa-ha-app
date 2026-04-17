#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

cleanup_migration_containers() {
    # Stop any casa-mig-*-$$ container that survived a mid-run failure so
    # port 18080 is freed before the next run.
    docker ps -q --filter "name=casa-mig-.*-$$" | xargs -r docker stop >/dev/null 2>&1 || true
}
trap cleanup_migration_containers EXIT

# Repo root is two levels up from test-local/e2e/
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

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

build_image

run_scenario() {
    local fixture="$1"
    local name="casa-mig-${fixture}-$$"

    # Copy the fixture to a tmp dir so the container's migrations don't
    # mutate the source of truth in git.
    local tmp
    tmp="${TMPBASE}/casa-mig-${fixture}-$$"
    mkdir -p "$tmp"
    cp -r "${REPO_ROOT}/test-local/fixtures/${fixture}/." "$tmp/"

    # MSYS_NO_PATHCONV=1 prevents Git Bash from mangling the /c/ host path
    # before handing it to docker.exe on Windows.
    MSYS_NO_PATHCONV=1 docker run -d --rm --name "$name" \
        -p "${HOST_PORT}:8080" \
        -v "${tmp}:/addon_configs/casa-agent" \
        "$IMAGE" >/dev/null
    wait_healthy "$name"

    # Return both the container name and the tmp dir so the caller can inspect.
    printf '%s %s\n' "$name" "$tmp"
}

inspect_and_stop() {
    local name="$1"
    local tmp="$2"

    # Collect post-migration state for assertions.
    local assistant="${tmp}/agents/assistant.yaml"
    local butler="${tmp}/agents/butler.yaml"
    local ellen="${tmp}/agents/ellen.yaml"
    local tina="${tmp}/agents/tina.yaml"

    echo "--- post-migration state ($name) ---"
    ls "${tmp}/agents/"
    [ -f "$assistant" ] && head -3 "$assistant" || true
    [ -f "$butler" ]    && head -3 "$butler"    || true

    stop_container "$name"
    # The container wrote files inside the volume as root (workspace/.claude,
    # workspace/plugins, ...). On Linux CI the runner user can't remove them,
    # so do the cleanup inside a throwaway root container before the host rm.
    # Any residual failure is ignored — the whole tmp tree dies with the
    # ephemeral runner anyway.
    docker run --rm -v "${tmp}:/target" --entrypoint sh "$IMAGE" \
        -c 'rm -rf /target/workspace /target/data /target/agents' \
        >/dev/null 2>&1 || true
    rm -rf "$tmp" 2>/dev/null || true
}

log "B-1: ellen.yaml -> assistant.yaml with role patch"
read NAME TMP < <(run_scenario legacy-ellen)
[ ! -f "${TMP}/agents/ellen.yaml" ] || fail "ellen.yaml survived the migration"
[ -f "${TMP}/agents/assistant.yaml" ] || fail "assistant.yaml was not created"
grep -q '^role: assistant$' "${TMP}/agents/assistant.yaml" \
    || fail "role was not patched to 'assistant'"
# peer_name was renormalised by the 2.1 migration and then stripped by
# the 2.2a memory-field migration. Post-2.2a, no peer_name line.
! grep -q '^  peer_name:' "${TMP}/agents/assistant.yaml" \
    || fail "peer_name survived the 2.2a migration"
inspect_and_stop "$NAME" "$TMP"
pass "B-1 ellen migration"

log "B-2: tina.yaml -> butler.yaml (no-op role, peer patch)"
read NAME TMP < <(run_scenario legacy-tina)
[ ! -f "${TMP}/agents/tina.yaml" ] || fail "tina.yaml survived the migration"
[ -f "${TMP}/agents/butler.yaml" ] || fail "butler.yaml was not created"
grep -q '^role: butler$' "${TMP}/agents/butler.yaml" \
    || fail "role was not 'butler' on migrated butler.yaml"
! grep -q '^  peer_name:' "${TMP}/agents/butler.yaml" \
    || fail "peer_name survived the 2.2a migration"
inspect_and_stop "$NAME" "$TMP"
pass "B-2 tina migration"

log "BUG-M1: custom role 'voice' is force-set to 'butler'"
read NAME TMP < <(run_scenario legacy-customrole)
grep -q '^role: butler$' "${TMP}/agents/butler.yaml" \
    || fail "custom role 'voice' was not force-reset to 'butler'"
grep -q '^role: voice' "${TMP}/agents/butler.yaml" \
    && fail "stale 'role: voice' still present"
inspect_and_stop "$NAME" "$TMP"
pass "BUG-M1 custom role force-reset"

log "BUG-M2: CRLF line endings migrate cleanly"
read NAME TMP < <(run_scenario legacy-crlf)
grep -q '^role: assistant' "${TMP}/agents/assistant.yaml" \
    || fail "CRLF-encoded role: main did not migrate to role: assistant"
# After strip + patch the file must have no stray CR chars.
if grep -q $'\r' "${TMP}/agents/assistant.yaml"; then
    fail "migrated file still contains CR characters"
fi
inspect_and_stop "$NAME" "$TMP"
pass "BUG-M2 CRLF handling"

log "B-4: re-running migration is a no-op when target exists"
read NAME TMP < <(run_scenario legacy-ellen)
# Snapshot content, then restart the container to re-trigger setup-configs.sh.
sha1_before=$(sha1sum "${TMP}/agents/assistant.yaml" | awk '{print $1}')
docker restart "$NAME" >/dev/null
wait_healthy "$NAME"
sha1_after=$(sha1sum "${TMP}/agents/assistant.yaml" | awk '{print $1}')
[ "$sha1_before" = "$sha1_after" ] \
    || fail "migration re-run mutated assistant.yaml"
inspect_and_stop "$NAME" "$TMP"
pass "B-4 idempotency"

log "B-5: pre-2.2a YAMLs lose peer_name + exclude_tags, gain read_strategy"
read NAME TMP < <(run_scenario legacy-pre22a)

# Assistant: peer_name removed, block-form exclude_tags removed,
# read_strategy: per_turn injected, token_budget preserved.
! grep -q '^  peer_name:' "${TMP}/agents/assistant.yaml" \
    || fail "assistant.yaml still has peer_name"
! grep -q '^  exclude_tags:' "${TMP}/agents/assistant.yaml" \
    || fail "assistant.yaml still has exclude_tags"
! grep -qE '^    - (private|financial)$' "${TMP}/agents/assistant.yaml" \
    || fail "assistant.yaml still has orphan exclude_tags list items"
grep -q '^  read_strategy: per_turn$' "${TMP}/agents/assistant.yaml" \
    || fail "assistant.yaml missing read_strategy: per_turn"
grep -q '^  token_budget: 4000$' "${TMP}/agents/assistant.yaml" \
    || fail "assistant.yaml lost its token_budget"

# Butler: peer_name removed, single-line exclude_tags removed,
# read_strategy: cached injected, token_budget preserved.
! grep -q '^  peer_name:' "${TMP}/agents/butler.yaml" \
    || fail "butler.yaml still has peer_name"
! grep -q '^  exclude_tags:' "${TMP}/agents/butler.yaml" \
    || fail "butler.yaml still has exclude_tags"
grep -q '^  read_strategy: cached$' "${TMP}/agents/butler.yaml" \
    || fail "butler.yaml missing read_strategy: cached"
grep -q '^  token_budget: 2000$' "${TMP}/agents/butler.yaml" \
    || fail "butler.yaml lost its token_budget"

# The migrated files must parse as valid 0.2.2 configs via the loader
# (it raises on unknown read_strategy; peer_name / exclude_tags are
# silently ignored since the dataclass no longer exposes them).
docker exec "$NAME" python3 -c "
import sys
sys.path.insert(0, '/opt/casa')
from config import load_agent_config
for f in ['/addon_configs/casa-agent/agents/assistant.yaml',
          '/addon_configs/casa-agent/agents/butler.yaml']:
    cfg = load_agent_config(f)
    print(cfg.role, cfg.memory.token_budget, cfg.memory.read_strategy)
" | tee /tmp/casa-pre22a-load.$$
grep -q '^assistant 4000 per_turn$' /tmp/casa-pre22a-load.$$ \
    || fail "assistant.yaml did not round-trip through load_agent_config"
grep -q '^butler 2000 cached$' /tmp/casa-pre22a-load.$$ \
    || fail "butler.yaml did not round-trip through load_agent_config"
rm -f /tmp/casa-pre22a-load.$$

# Idempotency: restart the container; migration must not duplicate lines.
sha_a1=$(sha1sum "${TMP}/agents/assistant.yaml" | awk '{print $1}')
sha_b1=$(sha1sum "${TMP}/agents/butler.yaml"    | awk '{print $1}')
docker restart "$NAME" >/dev/null
wait_healthy "$NAME"
sha_a2=$(sha1sum "${TMP}/agents/assistant.yaml" | awk '{print $1}')
sha_b2=$(sha1sum "${TMP}/agents/butler.yaml"    | awk '{print $1}')
[ "$sha_a1" = "$sha_a2" ] || fail "assistant.yaml changed on migration re-run"
[ "$sha_b1" = "$sha_b2" ] || fail "butler.yaml changed on migration re-run"
[ "$(grep -c '^  read_strategy:' "${TMP}/agents/assistant.yaml")" = "1" ] \
    || fail "assistant.yaml has duplicate read_strategy lines after re-run"
[ "$(grep -c '^  read_strategy:' "${TMP}/agents/butler.yaml")" = "1" ] \
    || fail "butler.yaml has duplicate read_strategy lines after re-run"

inspect_and_stop "$NAME" "$TMP"
pass "B-5 pre-2.2a memory migration + idempotency"
