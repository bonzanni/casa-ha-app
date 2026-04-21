#!/usr/bin/env bash
# Shared helpers for Casa E2E tests. Source this from each test script.
set -euo pipefail

IMAGE="${IMAGE:-casa-test}"
# Randomise the host port per-script run so back-to-back suites don't clash
# on a port Docker has not yet released (1-2s after docker stop).
HOST_PORT="${HOST_PORT:-$((18080 + RANDOM % 1000))}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-30}"

log()  { printf '[e2e] %s\n' "$*" >&2; }
fail() { printf '[e2e FAIL] %s\n' "$*" >&2; exit 1; }
pass() { printf '[e2e PASS] %s\n' "$*" >&2; }

build_image() {
    log "Building $IMAGE from test-local/Dockerfile.test"
    docker build -f test-local/Dockerfile.test -t "$IMAGE" . >/dev/null
}

# start_container <name> [extra docker args...]
# Prints the container id on stdout.
# Maps ${HOST_PORT}:8080 always; if EXT_PORT is set, also maps
# ${EXT_PORT}:18065 for tests that exercise the external server block.
start_container() {
    local name="$1"; shift
    local port_args=(-p "${HOST_PORT}:8080")
    if [ -n "${EXT_PORT:-}" ]; then
        port_args+=(-p "${EXT_PORT}:18065")
    fi
    docker run -d --rm --name "$name" \
        "${port_args[@]}" \
        "$@" "$IMAGE" >/dev/null
    echo "$name"
}

wait_healthy() {
    local name="$1"
    local i
    for i in $(seq 1 "$BOOT_TIMEOUT"); do
        if curl -sf "http://localhost:${HOST_PORT}/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    docker logs "$name" 2>&1 | tail -30 >&2
    fail "container $name never became healthy within ${BOOT_TIMEOUT}s"
}

stop_container() {
    local name="$1"
    docker stop "$name" >/dev/null 2>&1 || true
}

assert_log_contains() {
    # Poll docker logs for up to 15s — on CI, `docker logs` sometimes lags
    # behind the container's Python stdout even after healthz is green.
    local name="$1"
    local needle="$2"
    local deadline=$(( $(date +%s) + 15 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if docker logs "$name" 2>&1 | grep -qF "$needle"; then
            return 0
        fi
        sleep 0.5
    done
    docker logs "$name" 2>&1 | tail -30 >&2
    fail "expected log line '$needle' not found in $name"
}

assert_log_not_contains() {
    local name="$1"
    local needle="$2"
    if docker logs "$name" 2>&1 | grep -qF "$needle"; then
        fail "forbidden log line '$needle' found in $name"
    fi
}
