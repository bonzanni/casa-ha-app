#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

build_image

NAME="casa-invoke-$$"
trap "stop_container $NAME" EXIT

log "Starting container $NAME"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "C-3a: two /invoke calls with caller-supplied distinct chat_ids"
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"hi A","context":{"chat_id":"user-A"}}' >/dev/null \
    || fail "first /invoke failed"
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"hi B","context":{"chat_id":"user-B"}}' >/dev/null \
    || fail "second /invoke failed"

# Give the session-registry write a beat to flush.
sleep 1

log "Inspecting /data/sessions.json"
keys=$(docker exec "$NAME" sh -c \
    'python3 -c "import json,sys; print(\",\".join(sorted(json.load(open(\"/data/sessions.json\")).keys())))"')
echo "  registry keys: $keys"
echo "$keys" | grep -q "webhook:user-A" \
    || fail "missing 'webhook:user-A' entry"
echo "$keys" | grep -q "webhook:user-B" \
    || fail "missing 'webhook:user-B' entry"
echo "$keys" | grep -q "webhook:default" \
    && fail "unwanted 'webhook:default' entry present (BUG-I1 regression)"
pass "C-3a distinct caller-supplied chat_ids"

log "C-3b: two /invoke calls WITHOUT chat_id get unique UUIDs"
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"hi X"}' >/dev/null
curl -sf -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"hi Y"}' >/dev/null

sleep 1
webhook_keys=$(docker exec "$NAME" sh -c \
    'python3 -c "import json; d=json.load(open(\"/data/sessions.json\")); print(chr(10).join(k for k in d if k.startswith(\"webhook:\")))"')
uuid_count=$(echo "$webhook_keys" | grep -Ev "^webhook:(user-A|user-B)$" | grep -c "^webhook:" || true)
[ "$uuid_count" -eq 2 ] \
    || fail "expected 2 UUID-backed invoke sessions, got $uuid_count (keys: $webhook_keys)"
pass "C-3b each chat_id-less invoke gets its own session"

log "C-4: cc-home has 5 default plugins after boot (seed-copy verified)"
plugin_count=$(docker exec "$NAME" sh -c \
    'export HOME=/addon_configs/casa-agent/cc-home; \
     claude plugin list --json' \
    | python3 -c "import json,sys; d=json.load(sys.stdin); en=sum(1 for p in d if p.get('enabled')); print(f'{len(d)}/{en}')")
# Format is "<total>/<enabled>". Both must be 5 — count alone isn't enough,
# the binding layer (plugins_binding.py) filters out enabled=false plugins.
[ "$plugin_count" = "5/5" ] \
    || fail "expected 5/5 (total/enabled) default plugins from seed-copy, got $plugin_count"
pass "C-4 default plugins seeded: 5/5 (all enabled)"
