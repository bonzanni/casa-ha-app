#!/usr/bin/env bash
# test_ha_delegation.sh — verify v0.15.1 wiring: CASA_HA_MCP_URL env override
# threads through to MCP registration, butler.runtime.yaml's HA grant resolves
# through to a real HTTP call against mock HA MCP, and the voice-direct path
# triggers butler→HA without needing live model reasoning.
#
# Tier: 2 (functional). Runs on every push and PR.
#
# H-0  Boot mock HA MCP + Casa addon container with CASA_HA_MCP_URL override.
# H-1  Assert addon log shows `Registered Home Assistant MCP server (url=<mock>)`.
# H-2  Voice-direct: pre-write mock-SDK tool-invoke file, POST /api/converse
#      with agent_role=butler; assert mock HA /_calls gained 1 entry.
# H-3  Resident-options harness: docker exec a Python script that loads
#      butler via agent_loader and constructs SDK options like casa_core does;
#      run a query; assert mock HA /_calls gained another entry. Validates
#      the runtime.yaml→registry→options chain end-to-end.
#
# Coverage scope (per spec §4.2 / plan F.1.0):
# - This test does NOT exercise Ellen→delegate_to_agent→butler reasoning.
#   That two-hop chain requires the in-process casa-framework MCP server
#   (no URL) which the mock-SDK tool-invoke hook can't simulate. Real chain
#   coverage lives in J.5 manual smoke (live SDK + Anthropic key on N150).

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"
MOCK_HA_PORT="${MOCK_HA_PORT:-8200}"
MOCK_HA_PID=""
NAME="casa-ha-deleg-$$"

cleanup_all() {
    [ -n "$MOCK_HA_PID" ] && kill "$MOCK_HA_PID" 2>/dev/null || true
    docker stop "$NAME" >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

build_image

# ============================================================
# H-0: boot mock HA MCP + Casa addon
# ============================================================
log "H-0: start mock HA MCP + Casa addon"
python3 "$REPO_ROOT/test-local/e2e/mock_ha_mcp/server.py" --port "$MOCK_HA_PORT" \
    >/tmp/mock_ha_mcp.log 2>&1 &
MOCK_HA_PID=$!
for _ in $(seq 1 10); do
    curl -sf "http://localhost:${MOCK_HA_PORT}/_calls" >/dev/null 2>&1 && break
    sleep 0.5
done
curl -sf "http://localhost:${MOCK_HA_PORT}/_calls" >/dev/null \
    || fail "H-0: mock HA MCP not responding on port $MOCK_HA_PORT"

MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -e SUPERVISOR_TOKEN=test-token-v0151 \
    -e CASA_HA_MCP_URL="http://host.docker.internal:${MOCK_HA_PORT}/" \
    --add-host=host.docker.internal:host-gateway \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"
pass "H-0: mock HA MCP + addon up"

# ============================================================
# H-1: addon registered the mock HA URL (CASA_HA_MCP_URL flowed through)
# ============================================================
log "H-1: addon log mentions mock HA URL"
assert_log_contains "$NAME" \
    "Registered Home Assistant MCP server (url=http://host.docker.internal:${MOCK_HA_PORT}/)"
pass "H-1: CASA_HA_MCP_URL override threaded to register_http"

# ============================================================
# H-2: voice-direct path → butler → mock HA tool call
# ============================================================
log "H-2: voice/sse → butler → mock HA tool call"
curl -sf -X POST "http://localhost:${MOCK_HA_PORT}/_reset" >/dev/null

# Pre-seed the tool-invoke file so butler's mock-SDK call fires the HA tool.
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "echo '"'[{"server":"homeassistant","tool":"HassTurnOff","args":{"name":"kitchen"}}]'"' > /data/mock_sdk_tool_invoke.json"

curl -sf -N -X POST \
    "http://localhost:${HOST_PORT}/api/converse" \
    -H 'content-type: application/json' \
    -d '{"prompt":"turn off the kitchen lights","agent_role":"butler","scope_id":"v0151-h2"}' \
    --max-time 15 >/dev/null || true

# Wait until mock HA recorded the call (up to 15s).
deadline=$(( $(date +%s) + 15 ))
calls_count=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    calls_count=$(curl -sf "http://localhost:${MOCK_HA_PORT}/_calls" \
        | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
    [ "$calls_count" -ge 1 ] && break
    sleep 0.5
done

if [ "$calls_count" -lt 1 ]; then
    docker logs "$NAME" 2>&1 | tail -40 >&2
    fail "H-2: voice → butler did not hit mock HA (calls=$calls_count)"
fi

curl -s "http://localhost:${MOCK_HA_PORT}/_calls" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('called:', [c['name'] for c in d])"
pass "H-2: voice/sse → butler reached mock HA ($calls_count call(s))"

# ============================================================
# H-3: agent_loader → SDK options chain validates butler.runtime.yaml grant
# ============================================================
log "H-3: agent_loader resolves butler grant + SDK options reach mock HA"
curl -sf -X POST "http://localhost:${MOCK_HA_PORT}/_reset" >/dev/null

MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "echo '"'[{"server":"homeassistant","tool":"HassTurnOn","args":{"name":"bedroom"}}]'"' > /data/mock_sdk_tool_invoke.json"

read -r -d '' BUTLER_PY <<'PY' || true
import asyncio, os, sys
sys.path.insert(0, "/opt/casa")

from agent_loader import load_agent_from_dir
from policies import load_policies
from mcp_registry import McpServerRegistry
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions


async def main():
    # 1. Load butler from defaults — same path casa_core uses.
    policies = load_policies("/opt/casa/defaults/policies/disclosure.yaml")
    cfg = load_agent_from_dir(
        "/opt/casa/defaults/agents/butler",
        policies=policies,
    )
    assert "mcp__homeassistant" in cfg.tools.allowed, \
        f"butler.tools.allowed missing mcp__homeassistant: {cfg.tools.allowed}"
    assert "homeassistant" in cfg.mcp_server_names, \
        f"butler.mcp_server_names missing homeassistant: {cfg.mcp_server_names}"

    # 2. Build the same registry casa_core does and resolve butler's MCP servers.
    reg = McpServerRegistry()
    reg.register_http(
        name="homeassistant",
        url=os.environ.get("CASA_HA_MCP_URL"),
        headers={"Authorization": "Bearer test-token-v0151"},
    )
    resolved = reg.resolve(cfg.mcp_server_names)
    assert "homeassistant" in resolved and resolved["homeassistant"]["url"], \
        f"registry.resolve did not surface mock URL: {resolved}"

    # 3. Construct SDK options the way casa_core does for residents.
    opts = ClaudeAgentOptions(
        model=cfg.model or "haiku",
        system_prompt=cfg.system_prompt or "",
        allowed_tools=list(cfg.tools.allowed),
        mcp_servers=resolved,
        max_turns=1,
    )

    # 4. Run a query — mock SDK reads /data/mock_sdk_tool_invoke.json and
    #    fires the HTTP call against the resolved homeassistant URL.
    async with ClaudeSDKClient(opts) as client:
        await client.query("turn on the bedroom light")
        async for _msg in client.receive_response():
            pass

    print("OK")


asyncio.run(main())
PY

host_tmp="$(mktemp)"
printf '%s\n' "$BUTLER_PY" > "$host_tmp"
MSYS_NO_PATHCONV=1 docker cp "$host_tmp" "$NAME:/tmp/butler_resident_harness.py" >/dev/null
rm -f "$host_tmp"

if ! out=$(MSYS_NO_PATHCONV=1 docker exec \
        "$NAME" /opt/casa/venv/bin/python /tmp/butler_resident_harness.py 2>&1); then
    printf '%s\n' "$out" | tail -30 >&2
    fail "H-3: butler-resident harness exited non-zero"
fi
printf '%s\n' "$out" | tail -1 | grep -qF OK \
    || { printf '%s\n' "$out" | tail -10 >&2; fail "H-3: harness did not print OK"; }

calls=$(curl -sf "http://localhost:${MOCK_HA_PORT}/_calls" \
    | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
[ "$calls" -ge 1 ] \
    || fail "H-3: agent_loader→SDK chain did not reach mock HA (calls=$calls)"
pass "H-3: agent_loader → registry → SDK options → mock HA chain works ($calls call(s))"

stop_container "$NAME"
log "All H-* checkpoints green."
