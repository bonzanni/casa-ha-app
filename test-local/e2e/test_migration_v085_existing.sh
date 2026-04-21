#!/usr/bin/env bash
# v0.8.5 migration scenario: an existing instance with an old overlay
# must (a) get the new defaults copied in, (b) gain the marker file,
# (c) keep its old content as a `.pre-v0.8.5.bak` backup, and (d) NOT
# re-run the migration on a second boot.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-mig85-$$"
trap "stop_container $NAME" EXIT

build_image

log "Phase A: boot once (seed fresh defaults), then mutate the overlay"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

# Wipe the seeded scopes.yaml and replace with a sentinel that
# represents "old overlay shape" — anything different from defaults.
# Must satisfy the schema (minLength=20 per description, all four
# scopes referenced by agent runtime.yamls present) so the container
# can boot before the migration runs on Phase B's restart. Use a
# here-doc rather than `echo "...\n..."` — BusyBox and dash disagree
# on literal `\n` handling, and the schema parser expects real newlines.
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c 'cat > /addon_configs/casa-agent/policies/scopes.yaml <<EOF
schema_version: 1
scopes:
  personal:
    minimum_trust: authenticated
    description: old prose sentinel for personal scope pre-migration
  business:
    minimum_trust: authenticated
    description: old prose sentinel for business scope pre-migration
  finance:
    minimum_trust: authenticated
    description: old prose sentinel for finance scope pre-migration
  house:
    minimum_trust: household-shared
    description: old prose sentinel for house scope pre-migration
EOF'

# Remove the marker if it exists (so we can re-test the migration path).
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    rm -f /addon_configs/casa-agent/migrations/scope_corpus_v0.8.5.applied

log "Phase B: restart container; migration must fire"
docker restart "$NAME" >/dev/null
wait_healthy "$NAME"

log "E-1: marker file now present"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test -f /addon_configs/casa-agent/migrations/scope_corpus_v0.8.5.applied \
    || fail "marker missing after migration boot"
pass "marker present"

log "E-2: scopes.yaml now matches defaults"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    diff -q /addon_configs/casa-agent/policies/scopes.yaml \
            /opt/casa/defaults/policies/scopes.yaml \
    || fail "scopes.yaml not refreshed by migration"
pass "scopes.yaml refreshed"

log "E-3: backup file contains the old content"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    grep -q "old prose" \
        /addon_configs/casa-agent/policies/scopes.yaml.pre-v0.8.5.bak \
    || fail "backup missing or wrong content"
pass "backup preserved"

log "Phase C: third boot must NOT re-run the migration (marker honored)"
# Mutate scopes.yaml again — a rerun would clobber this.
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    'echo "# user manually edited" >> /addon_configs/casa-agent/policies/scopes.yaml'
docker restart "$NAME" >/dev/null
wait_healthy "$NAME"

log "E-4: user edit preserved across reboot (marker prevented re-run)"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    grep -q "user manually edited" \
        /addon_configs/casa-agent/policies/scopes.yaml \
    || fail "migration re-ran on subsequent boot — marker not honored"
pass "marker prevented re-run"

log "All v0.8.5 existing-instance migration checks passed."
