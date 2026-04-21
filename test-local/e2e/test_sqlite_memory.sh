#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# Mirror the _tmpbase pattern from test_migration.sh so /data is
# Docker-visible on Windows dev hosts.
_tmpbase() {
    local wintemp
    wintemp="$(powershell.exe -NoProfile -Command \
        "[System.IO.Path]::GetTempPath()" 2>/dev/null | tr -d '\r\n' || true)"
    if [ -n "$wintemp" ]; then
        local drive="${wintemp:0:1}"
        local rest="${wintemp:2}"
        rest="$(printf '%s' "$rest" | tr '\\' '/')"
        printf '/%s%s' "$(printf '%s' "$drive" | tr '[:upper:]' '[:lower:]')" "$rest"
    else
        printf '/tmp'
    fi
}

TMPBASE="$(_tmpbase)"
DATA_DIR="${TMPBASE}/casa-sqlite-$$"
mkdir -p "$DATA_DIR"

NAME="casa-sqlite-$$"
cleanup() {
    stop_container "$NAME" || true
    docker run --rm -v "${DATA_DIR}:/target" --entrypoint sh "$IMAGE" \
        -c 'rm -rf /target/memory.sqlite /target/memory.sqlite-wal /target/memory.sqlite-shm /target/sessions.json /target/sdk-sessions /target/options.json /target/webhook_secret' \
        >/dev/null 2>&1 || true
    rm -rf "$DATA_DIR" 2>/dev/null || true
}
trap cleanup EXIT

build_image

# The test image bakes options.json into /data; a bind-mount would
# shadow it and crash setup-configs. Pre-populate options.json in the
# host dir from the repo fixture, then mount it.
cp "${REPO_ROOT}/test-local/options.json.example" "${DATA_DIR}/options.json"

log "Starting container $NAME with volume-mounted /data (no HONCHO_API_KEY)"
# Empty HONCHO_API_KEY → spec §2 step 3 picks SQLite.
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -v "${DATA_DIR}:/data" \
    -e "HONCHO_API_KEY=" \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"

assert_log_contains "$NAME" "SQLite memory provider initialized"
pass "SQLite picked as default on fresh install"

log "Turn 1 — /invoke records a row into memory.sqlite"
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/butler" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"E2E-MARKER-ONE","context":{"chat_id":"e2e"}}' >/dev/null \
    || fail "first /invoke failed"
# add_turn is fired on a background task — give it a moment.
sleep 2

count_one=$(docker exec "$NAME" python3 -c "
import sqlite3
c = sqlite3.connect('/data/memory.sqlite')
print(c.execute(\"SELECT COUNT(*) FROM messages WHERE content LIKE '%E2E-MARKER-ONE%'\").fetchone()[0])
")
[ "$count_one" -ge "1" ] \
    || fail "turn 1 did not persist — expected >=1 row, got $count_one"
pass "turn 1 persisted to SQLite"

log "Restarting container — SQLite file must survive"
docker restart "$NAME" >/dev/null
wait_healthy "$NAME"

count_after_restart=$(docker exec "$NAME" python3 -c "
import sqlite3
c = sqlite3.connect('/data/memory.sqlite')
print(c.execute(\"SELECT COUNT(*) FROM messages WHERE content LIKE '%E2E-MARKER-ONE%'\").fetchone()[0])
")
[ "$count_after_restart" -ge "1" ] \
    || fail "turn 1 row vanished across restart — got $count_after_restart"
pass "turn 1 survived restart"

log "Turn 2 — appending a second turn after restart"
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/butler" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"E2E-MARKER-TWO","context":{"chat_id":"e2e"}}' >/dev/null \
    || fail "second /invoke failed"
sleep 2

count_two=$(docker exec "$NAME" python3 -c "
import sqlite3
c = sqlite3.connect('/data/memory.sqlite')
one = c.execute(\"SELECT COUNT(*) FROM messages WHERE content LIKE '%E2E-MARKER-ONE%'\").fetchone()[0]
two = c.execute(\"SELECT COUNT(*) FROM messages WHERE content LIKE '%E2E-MARKER-TWO%'\").fetchone()[0]
print(f'{one} {two}')
")
first=$(echo "$count_two" | awk '{print $1}')
second=$(echo "$count_two" | awk '{print $2}')
[ "$first" -ge "1" ] || fail "turn 1 lost after turn 2 (got $first)"
[ "$second" -ge "1" ] || fail "turn 2 missing (got $second)"
pass "both turns persisted across restart"
