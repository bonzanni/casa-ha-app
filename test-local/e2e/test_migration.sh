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
grep -q '^  peer_name: assistant$' "${TMP}/agents/assistant.yaml" \
    || fail "peer_name was not patched to 'assistant'"
inspect_and_stop "$NAME" "$TMP"
pass "B-1 ellen migration"

log "B-2: tina.yaml -> butler.yaml (no-op role, peer patch)"
read NAME TMP < <(run_scenario legacy-tina)
[ ! -f "${TMP}/agents/tina.yaml" ] || fail "tina.yaml survived the migration"
[ -f "${TMP}/agents/butler.yaml" ] || fail "butler.yaml was not created"
grep -q '^role: butler$' "${TMP}/agents/butler.yaml" \
    || fail "role was not 'butler' on migrated butler.yaml"
grep -q '^  peer_name: butler$' "${TMP}/agents/butler.yaml" \
    || fail "peer_name was not patched to 'butler'"
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
