#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

build_image

NAME="casa-conc-$$"
trap "stop_container $NAME" EXIT

log "Starting container with MOCK_SDK_LATENCY_SEC=2"
start_authed_container "$NAME" -e MOCK_SDK_LATENCY_SEC=2 >/dev/null
wait_healthy "$NAME"

log "D-1: firing two /invoke calls in parallel"
start_ts=$(now_ms)
signed_invoke "$HOST_PORT" assistant '{"prompt":"slow-A","context":{"chat_id":"A"}}' >/dev/null &
signed_invoke "$HOST_PORT" assistant '{"prompt":"slow-B","context":{"chat_id":"B"}}' >/dev/null &
wait
end_ts=$(now_ms)
elapsed=$(( end_ts - start_ts ))
log "elapsed: ${elapsed}ms"

# Sanity-check the CLOCK before judging the PRODUCT (#271). This must run
# before the two bounds below: a broken stamp lands far outside them, and the
# messages there blame the semaphore, so a harness fault would read as a
# concurrency regression.
if [ "$elapsed" -lt 0 ] || [ "$elapsed" -gt 600000 ]; then
    fail "implausible elapsed ${elapsed}ms — the harness clock is wrong, not the app (check date(1) %N support)"
fi

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
