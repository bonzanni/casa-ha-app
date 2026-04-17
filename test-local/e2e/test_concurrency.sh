#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

build_image

NAME="casa-conc-$$"
trap "stop_container $NAME" EXIT

log "Starting container with MOCK_SDK_LATENCY_SEC=2"
start_container "$NAME" -e MOCK_SDK_LATENCY_SEC=2 >/dev/null
wait_healthy "$NAME"

log "D-1: firing two /invoke calls in parallel"
start_ts=$(date +%s%3N)
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"slow-A","context":{"chat_id":"A"}}' >/dev/null &
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"slow-B","context":{"chat_id":"B"}}' >/dev/null &
wait
end_ts=$(date +%s%3N)
elapsed=$(( end_ts - start_ts ))
log "elapsed: ${elapsed}ms"

# Phase 2.1 has no semaphore, so we expect concurrent execution (~2s).
# If someone adds MAX_CONCURRENT_AGENTS=1 upstream, this test must be
# updated to expect ~4s.
if [ "$elapsed" -lt 1800 ]; then
    fail "elapsed ${elapsed}ms < 1800ms — SDK latency not honoured?"
fi
if [ "$elapsed" -gt 3500 ]; then
    fail "elapsed ${elapsed}ms > 3500ms — calls appear serialised (check semaphore config)"
fi
pass "D-1 two concurrent invokes (${elapsed}ms)"
