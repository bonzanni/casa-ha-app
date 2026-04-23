#!/usr/bin/env bash
# Plan 4b/3.6 — mid-restart survival e2e test.
#
# Validates the headline guarantee of v0.14.0: bouncing casa-main
# (s6-rc -d/-u change svc-casa) does NOT take svc-casa-mcp down or sever
# the tool-call surface. Mid-restart tool calls return JSON-RPC
# -32000 casa_temporarily_unavailable; post-restart calls succeed.
#
# Mock-CLI gated (CASA_USE_MOCK_CLAUDE=1). Auto-skips otherwise.

set -euo pipefail

if [ "${CASA_USE_MOCK_CLAUDE:-0}" != "1" ]; then
    echo "SKIP: CASA_USE_MOCK_CLAUDE=1 required"
    exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/common.sh"

build_image_with_mock_cli

NAME="casa-mcp-restart-$$"
start_container "$NAME"
trap "stop_container '$NAME' >/dev/null 2>&1 || true" EXIT

wait_healthy "$NAME"

echo "=== M-1: confirm svc-casa-mcp is bound on 8100 ==="
RESP=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
    http://127.0.0.1:8100/mcp/casa-framework)
echo "  initialize: $RESP"
echo "$RESP" | grep -q '"name":"casa-framework"' \
    || fail "M-1 svc-casa-mcp not responding on 8100"
pass "M-1 svc-casa-mcp bound on 8100"

echo "=== M-2: tool call before bounce — expect success ==="
PRE=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_engagement_workspaces","arguments":{}}}' \
    http://127.0.0.1:8100/mcp/casa-framework)
echo "  pre-bounce: $PRE"
echo "$PRE" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d.get("result") is not None, d' \
    || fail "M-2 expected .result; got: $PRE"
pass "M-2 pre-bounce tool call succeeded"

echo "=== M-3: bounce casa-main (svc-casa down) ==="
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -d change svc-casa
sleep 1

echo "=== M-4: tool call during bounce — expect -32000 ==="
DOWN=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_engagement_workspaces","arguments":{}}}' \
    http://127.0.0.1:8100/mcp/casa-framework)
echo "  during-bounce: $DOWN"
echo "$DOWN" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d.get("error", {}).get("code") == -32000, d' \
    || fail "M-4 expected error.code -32000; got: $DOWN"
pass "M-4 mid-bounce returned casa_temporarily_unavailable"

echo "=== M-5: bring casa-main back up + wait for socket ==="
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -u change svc-casa
# Poll for casa-main's Unix socket to come back (up to 12s).
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S /run/casa/internal.sock; then
        echo "  socket ready after ${i}s"
        break
    fi
    sleep 1
done

echo "=== M-6: tool call after bounce — expect success ==="
POST=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_engagement_workspaces","arguments":{}}}' \
    http://127.0.0.1:8100/mcp/casa-framework)
echo "  post-bounce: $POST"
echo "$POST" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d.get("result") is not None, d' \
    || fail "M-6 expected .result; got: $POST"
pass "M-6 post-bounce tool call succeeded"

echo "=== ALL PASS — mcp_restart_survival ==="
