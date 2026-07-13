#!/usr/bin/env bash
# Plan 4b P-block: plugin-developer + Configurator install flow (P-1..P-9).
# Tier-2 functional: registry + workspace-provisioning assertions, no
# timing/chaos. Cheap once the mock-CLI image is cached.
# Requires CASA_USE_MOCK_CLAUDE=1 + CASA_PLAN_4B=1.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"

MOCK_PORT=8081
SUPERGROUP_ID=-1001
NAME="casa-eng-p-$$"

cleanup_all() {
    docker ps -q --filter "name=casa-eng-p-.*-$$" | xargs -r docker stop >/dev/null 2>&1 || true
    docker stop "$NAME" >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

# Skip the entire script unless BOTH gates are explicitly enabled.
# Preserves original test_engagement.sh behaviour: P-block was nested
# inside the D-block (CASA_USE_MOCK_CLAUDE=1) and additionally gated by
# CASA_PLAN_4B=1 in `if [ "${CASA_PLAN_4B:-0}" = "1" ]; then`.
if [ "${CASA_USE_MOCK_CLAUDE:-0}" != "1" ]; then
    log "P-block skipped (set CASA_USE_MOCK_CLAUDE=1 to enable)"
    echo "=== test_engagement_P.sh complete (skipped: mock CLI) ==="
    exit 0
fi
if [ "${CASA_PLAN_4B:-0}" != "1" ]; then
    log "P-block skipped (set CASA_PLAN_4B=1 to enable)"
    echo "=== test_engagement_P.sh complete (skipped: plan 4b) ==="
    exit 0
fi

# ---------------------------------------------------------------------------
# Helper: run_harness <label> <py>
# Copies the python source into /tmp in the container and runs it inside
# the casa venv. Fails (assert_contains style) if the harness's last line
# is not "OK".
# ---------------------------------------------------------------------------
run_harness() {
    local label="$1"; shift
    local py="$1"; shift
    local host_tmp
    host_tmp="$(mktemp)"
    printf '%s\n' "$py" > "$host_tmp"
    MSYS_NO_PATHCONV=1 docker cp "$host_tmp" "$NAME:/tmp/_harness.py" >/dev/null
    rm -f "$host_tmp"
    local out
    if ! out=$(MSYS_NO_PATHCONV=1 docker exec -e MOCK_TG_BASE="http://host.docker.internal:${MOCK_PORT}" \
               -e SUPERGROUP_ID="$SUPERGROUP_ID" \
               "$NAME" /opt/casa/venv/bin/python /tmp/_harness.py 2>&1); then
        printf '%s\n' "$out" >&2
        fail "$label harness exited non-zero"
    fi
    printf '%s\n' "$out" | tail -20 >&2
    printf '%s\n' "$out" | tail -1 | grep -qF "OK" \
        || fail "$label harness did not print OK as final line"
}

# Build the casa-test image with the mock CLI overlaid (same image-tag as
# D-block; if D ran first in the same shell, this is a no-op cache hit).
build_image_with_mock_cli

# Boot a dedicated container for P-block. Mock TG is not needed.
log "P-block: boot container for plugin-developer harness"
MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -e TELEGRAM_ENGAGEMENT_SUPERGROUP_ID="$SUPERGROUP_ID" \
    -e TELEGRAM_BOT_API_BASE="http://host.docker.internal:${MOCK_PORT}" \
    --add-host=host.docker.internal:host-gateway \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"
pass "P-block container healthy"

log "P-1: plugin-developer executor plugins load via --plugin-dir (§3.8)"
run_harness "P-1" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")

# §3.3/§3.8: executor plugins are NOT provisioned into settings.json anymore.
# They resolve from the registry (resolve_for(executor:<type>) — the exact
# call tools.py makes at engagement launch) to immutable store paths, which
# the driver renders as repeated --plugin-dir flags on the run script.
import plugin_registry
from drivers.workspace import render_run_script

res = plugin_registry.resolve_for("executor:plugin-developer")
assert res.registry_valid, (
    f"registry invalid: {[i.reason_code for i in res.issues]}")
names = {p.name for p in res.plugins}
assert "superpowers" in names, (
    f"superpowers not resolved for plugin-developer: {sorted(names)}")

sp = next(p for p in res.plugins if p.name == "superpowers")
assert sp.path.startswith("/config/plugins/store/superpowers/"), (
    f"superpowers store path unexpected: {sp.path}")

script = render_run_script(
    engagement_id="p1test00000000000000000000000001",
    permission_mode="default",
    extra_dirs=[],
    plugin_dirs=[p.path for p in res.plugins],
)
assert "--plugin-dir /config/plugins/store/superpowers/" in script, (
    f"--plugin-dir superpowers flag missing from run script:\n{script}")
print("P-1 OK")
PY
)"
pass "P-1: plugin-developer run script pins plugins via --plugin-dir"

# ---------------------------------------------------------------------------
# P-2 — mock gh CLI present
# ---------------------------------------------------------------------------
log "P-2: mock gh repo create handler present"
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    'which gh >/dev/null 2>&1 && echo "P-2 OK (gh CLI present in image)" || echo "P-2 SKIP (gh not in image)"' \
    | grep -qE "P-2 (OK|SKIP)" && pass "P-2: gh CLI check done" \
    || fail "P-2: gh CLI check failed unexpectedly"

# ---------------------------------------------------------------------------
# P-3 — emit_completion tool is present in the CASA_TOOLS registry
# ---------------------------------------------------------------------------
log "P-3: emit_completion tool callable"
run_harness "P-3" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
from tools import CASA_TOOLS
names = [t.name if hasattr(t, "name") else t.get("name", str(t)) for t in CASA_TOOLS]
assert "emit_completion" in names, f"emit_completion missing from CASA_TOOLS: {names}"
print("P-3 OK")
PY
)"
pass "P-3: emit_completion present in CASA_TOOLS"

# ---------------------------------------------------------------------------
# P-4 — plugin_add tool registered (§3.13; replaces install_casa_plugin)
# ---------------------------------------------------------------------------
log "P-4: Configurator plugin_add tool registered"
run_harness "P-4" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
import tools
names = {getattr(t, "name", None) for t in tools.CASA_TOOLS}
assert "plugin_add" in names, f"plugin_add not in CASA_TOOLS: {sorted(n for n in names if n)}"
assert hasattr(tools, "plugin_add"), "plugin_add tool object missing from module"
assert hasattr(tools, "_plugin_add_sync"), "_plugin_add_sync core missing"
print("P-4 OK")
PY
)"
pass "P-4: plugin_add registered in CASA_TOOLS"

# ---------------------------------------------------------------------------
# P-5 — plugin_remove tool registered (§3.13; replaces uninstall_casa_plugin)
# ---------------------------------------------------------------------------
log "P-5: Configurator plugin_remove tool registered"
run_harness "P-5" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
import tools
names = {getattr(t, "name", None) for t in tools.CASA_TOOLS}
assert "plugin_remove" in names, f"plugin_remove not in CASA_TOOLS: {sorted(n for n in names if n)}"
assert hasattr(tools, "plugin_remove"), "plugin_remove tool object missing from module"
assert hasattr(tools, "_plugin_remove_sync"), "_plugin_remove_sync core missing"
print("P-5 OK")
PY
)"
pass "P-5: plugin_remove registered in CASA_TOOLS"

# ---------------------------------------------------------------------------
# P-6 — plugin_remove refuses an unregistered plugin (registry guard; the
#        marketplace read-only guard's successor). Uses the sync core on a
#        name NOT in the registry so the live registry is never mutated.
# ---------------------------------------------------------------------------
log "P-6: plugin_remove refuses an unregistered plugin"
run_harness "P-6" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
from tools import _plugin_remove_sync

r = _plugin_remove_sync(name="p6-definitely-not-registered")
assert r.get("ok") is False, f"expected ok=False, got {r}"
assert r.get("kind") == "not_registered", f"expected not_registered, got {r}"
print("P-6 OK (unregistered plugin refused)")
PY
)"
pass "P-6: plugin_remove refuses an unregistered plugin (not_registered)"

# ---------------------------------------------------------------------------
# P-7 — tarball systemRequirements infrastructure importable
# ---------------------------------------------------------------------------
log "P-7: tarball systemRequirements infrastructure present"
run_harness "P-7" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
from system_requirements.tarball import install_tarball, IntegrityError
from system_requirements.orchestrator import install_requirements
print("P-7 OK")
PY
)"
pass "P-7: system_requirements tarball + orchestrator importable"

# ---------------------------------------------------------------------------
# P-8 — upgrade survival: reconcile_system_requirements.py present + --help
# ---------------------------------------------------------------------------
log "P-8: upgrade survival — reconciler infrastructure"
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f \
    /opt/casa/scripts/reconcile_system_requirements.py \
    || fail "P-8: reconcile_system_requirements.py missing"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    /opt/casa/venv/bin/python /opt/casa/scripts/reconcile_system_requirements.py --help \
    >/dev/null 2>&1 \
    || fail "P-8: reconcile_system_requirements.py --help failed"
pass "P-8: reconcile_system_requirements.py present and --help exits cleanly"

# ---------------------------------------------------------------------------
# P-9 — self_containment_guard blocks anti-pattern push
# ---------------------------------------------------------------------------
log "P-9: self_containment_guard blocks anti-pattern push"
run_harness "P-9" "$(cat <<'PY'
import asyncio, sys, tempfile
sys.path.insert(0, "/opt/casa")
from pathlib import Path
from hooks import make_self_containment_guard

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "fake-repo"
        repo.mkdir()
        (repo / ".claude-plugin").mkdir()
        (repo / ".claude-plugin" / "plugin.json").write_text("{}")
        (repo / "README.md").write_text("please install ffmpeg manually\n")

        hook = make_self_containment_guard()
        res = await hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"},
                "cwd": str(repo),
            },
            None,
            {},
        )
        assert res is not None, (
            "self_containment_guard returned None — hook must deny the push"
        )
        spec = res.get("hookSpecificOutput", {})
        assert spec.get("permissionDecision") == "deny", (
            f"expected deny, got: {spec}"
        )
    print("P-9 OK")

asyncio.run(main())
PY
)"
pass "P-9: self_containment_guard denies push with anti-pattern README"

# ---------------------------------------------------------------------------
# P-10 — real-artifact authorization proof (Sol R2-5): verify_plugin_state on
#         the REAL materialized context7 bundle × the REAL plugin-developer
#         definition reports no missing authorization for its namespace.
#         Sol R3: plugin-developer ships enabled:false, so the running
#         registry's get() returns None — load the definition directly
#         (load_all_executors returns ALL defns pre-enabled-filter) and wire
#         it into active_runtime, exactly the definition the addon uses.
# ---------------------------------------------------------------------------
log "P-10: verify_plugin_state(context7) authorizes plugin-developer namespace"
run_harness "P-10" "$(cat <<'PY'
import sys, types
sys.path.insert(0, "/opt/casa")
import agent as agent_mod
from agent_loader import load_all_executors
from tools import _tool_verify_plugin_state

found, failed = load_all_executors("/config/agents")
defn = found.get("plugin-developer")
assert defn is not None, f"plugin-developer defn not loaded; failed={failed}"

class _Reg:
    def get(self, name):
        return defn if name == "plugin-developer" else None

agent_mod.active_runtime = types.SimpleNamespace(agents={}, executor_registry=_Reg())

state = _tool_verify_plugin_state(plugin_name="context7")
# The grant must actually derive from the materialized artifact's .mcp.json —
# guards against a vacuous empty-grants pass.
assert "mcp__plugin_context7_context7" in state["granted_tools"], (
    f"context7 grant not derived from artifact: {state.get('granted_tools')} "
    f"reasons={state.get('reasons')}")
rows = [r for r in state["targets"] if r["target"] == "executor:plugin-developer"]
assert rows, f"no executor:plugin-developer target row: {state['targets']}"
missing = rows[0].get("authorization", {}).get("missing")
assert missing == [], (
    f"context7 authorization_missing for plugin-developer: {rows[0]}")
print("P-10 OK")
PY
)"
pass "P-10: context7 namespace authorized by real plugin-developer definition"

stop_container "$NAME"

echo "=== test_engagement_P.sh complete ==="
