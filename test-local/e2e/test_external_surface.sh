#!/usr/bin/env bash
# 5.7: external-surface contract — public :18065 exposes /healthz
# only; / returns 404; ingress :$INGRESS_PORT → / stays 200.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

NAME="casa-ext-surface-$$"
# Random external port per-run — mirrors HOST_PORT in common.sh so
# back-to-back runs don't clash on a port Docker hasn't released yet.
EXT_PORT="${EXT_PORT:-$((19080 + RANDOM % 1000))}"
export EXT_PORT
trap "stop_container $NAME" EXIT

build_image

log "Starting container $NAME (ingress :${HOST_PORT}, external :${EXT_PORT})"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "5.7-1: ingress GET / returns 200 (dashboard alive)"
curl -sf "http://localhost:${HOST_PORT}/" >/dev/null \
    || fail "ingress / did not return 200"
pass "ingress / OK"

log "5.7-2: external GET / returns 404 (public dashboard closed)"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${EXT_PORT}/")
[ "$code" = "404" ] || fail "external / returned $code, expected 404"
pass "external / returns 404"

log "5.7-3: external GET /healthz returns 200 (uptime contract)"
curl -sf "http://localhost:${EXT_PORT}/healthz" >/dev/null \
    || fail "external /healthz did not return 200"
pass "external /healthz OK"
