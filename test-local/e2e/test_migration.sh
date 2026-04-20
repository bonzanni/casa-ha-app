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
