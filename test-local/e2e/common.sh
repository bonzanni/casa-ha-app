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

# Build a test-only image that overrides /usr/bin/claude with the mock CLI
# from test-local/mock-claude-cli/claude. Used by the D-block engagement
# tests in test_engagement.sh when CASA_USE_MOCK_CLAUDE=1.
build_image_with_mock_cli() {
    local repo_root
    repo_root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
    local tag="$IMAGE"
    build_image          # build the standard Casa image first

    # Overlay the mock CLI on top with a tiny derivative Dockerfile.
    local derivative
    derivative="$(mktemp -d)"
    cat > "$derivative/Dockerfile" <<EOF
FROM ${tag}
COPY mock-claude /usr/bin/claude
RUN chmod +x /usr/bin/claude
EOF
    cp "$repo_root/test-local/mock-claude-cli/claude" "$derivative/mock-claude"
    MSYS_NO_PATHCONV=1 docker build -q -t "$tag" "$derivative" >/dev/null
    rm -rf "$derivative"
    log "Overlaid mock claude CLI on $tag"
}

# wait_for_text_in_log <container> <pattern> [timeout_s]
# Polls `docker logs` until *pattern* (grep -E, fixed regex ok) appears.
# Returns 0 on match, 1 on timeout. Unlike assert_log_contains this never
# fails the suite — callers decide what to do.
wait_for_text_in_log() {
    local name="$1"
    local pattern="$2"
    local timeout="${3:-30}"
    local end=$(( $(date +%s) + timeout ))
    while [ "$(date +%s)" -lt "$end" ]; do
        if docker logs "$name" 2>&1 | grep -Eq "$pattern"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# assert_file_contains <container> <path> <needle> <label>
# Fails the suite if *needle* is not in <path> inside <container>.
assert_file_contains() {
    local name="$1"
    local path="$2"
    local needle="$3"
    local label="$4"
    if MSYS_NO_PATHCONV=1 docker exec "$name" grep -qF "$needle" "$path"; then
        pass "$label"
    else
        MSYS_NO_PATHCONV=1 docker exec "$name" cat "$path" 2>&1 | tail -20 >&2 || true
        fail "$label (grep for '$needle' in $path failed)"
    fi
}

# start_mock_telegram_server [port]
# Spawns the mock Telegram server backing the engagement e2e tests.
# Echoes the PID of the spawned python process on stdout (caller traps cleanup).
# Honors $MOCK_TG_PORT (default 8081) so multiple suites can pick distinct
# ports if ever run in parallel.
# Returns 0 on success, 1 on timeout (after which caller should fail loudly).
start_mock_telegram_server() {
    local port="${1:-${MOCK_TG_PORT:-8081}}"
    local repo_root
    repo_root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
    python3 "$repo_root/test-local/e2e/mock_telegram/server.py" \
        >/tmp/mock-tg.log 2>&1 &
    local pid=$!
    local i
    for i in $(seq 1 10); do
        if curl -sf "http://localhost:${port}/_inspect" >/dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
        sleep 0.3
    done
    kill "$pid" 2>/dev/null || true
    return 1
}
