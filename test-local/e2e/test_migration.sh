#!/usr/bin/env bash
# Post-cut migration scenario. The 0.7.0 cut removed every one-shot YAML
# migration from setup-configs.sh; this script now verifies the
# replacement story: directory-based seed-copy + git-init + shared
# policy library seed. A fresh container boots with an empty config dir
# and must end up with the bundled directory tree in place.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-migration-$$"
trap "stop_container $NAME" EXIT

build_image

log "Starting container $NAME"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "M-1: agents/butler/character.yaml seeded"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test -f /addon_configs/casa-agent/agents/butler/character.yaml \
    || fail "butler/character.yaml not seeded"
pass "butler dir seeded"

log "M-2: agents/assistant/disclosure.yaml seeded"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test -f /addon_configs/casa-agent/agents/assistant/disclosure.yaml \
    || fail "assistant/disclosure.yaml not seeded"
pass "assistant dir seeded"

log "M-3: policies/disclosure.yaml seeded"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test -f /addon_configs/casa-agent/policies/disclosure.yaml \
    || fail "policies/disclosure.yaml not seeded"
pass "policies seeded"

log "M-4: .git repo initialized"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test -d /addon_configs/casa-agent/.git \
    || fail ".git directory missing"
pass ".git initialized"

log "M-5: initial commit exists"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    git -C /addon_configs/casa-agent log --oneline \
    | grep -q "initial config snapshot" \
    || fail "initial config snapshot commit missing"
pass "initial commit present"

log "M-7: v0.8.5 scope migration marker NOT present on fresh install"
# Fresh install path: scopes.yaml was seeded from defaults, never
# migrated, so the marker should be absent. Migration only fires when
# an *existing* overlay is found at boot.
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test ! -f /addon_configs/casa-agent/migrations/scope_corpus_v0.8.5.applied \
    || fail "v0.8.5 migration marker should not exist on fresh install"
pass "v0.8.5 migration marker absent on fresh install"

log "M-8: scopes.yaml content matches shipped defaults"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    diff -q /addon_configs/casa-agent/policies/scopes.yaml \
            /opt/casa/defaults/policies/scopes.yaml \
    || fail "fresh-install scopes.yaml does not match shipped defaults"
pass "fresh-install scopes.yaml matches shipped defaults"

log "M-9: backup file NOT present on fresh install"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test ! -f /addon_configs/casa-agent/policies/scopes.yaml.pre-v0.8.5.bak \
    || fail "v0.8.5 backup file should not exist on fresh install"
pass "v0.8.5 backup file absent on fresh install"
