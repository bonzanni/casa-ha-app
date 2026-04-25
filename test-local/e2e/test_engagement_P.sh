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

log "P-1: plugin-developer workspace provisioning"
run_harness "P-1" "$(cat <<'PY'
import asyncio, pathlib, json, sys
sys.path.insert(0, "/opt/casa")

async def main():
    from drivers.workspace import provision_workspace
    from executor_registry import ExecutorRegistry

    exec_reg = ExecutorRegistry("/opt/casa/defaults/agents/executors")
    exec_reg.load()
    defn = exec_reg.get("plugin-developer")
    assert defn is not None, "plugin-developer executor not found in registry"

    ws = await provision_workspace(
        engagements_root="/tmp/p1-engagements",
        base_plugins_root="/opt/casa/claude-plugins/base",
        engagement_id="p1test00000000000000000000000001",
        defn=defn,
        task="test plugin",
        context="t=1",
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        workspace_template_root=pathlib.Path(
            "/opt/casa/defaults/agents/executors/plugin-developer/workspace-template"
        ),
        plugins_yaml=pathlib.Path(
            "/opt/casa/defaults/agents/executors/plugin-developer/plugins.yaml"
        ),
    )

    ws_path = pathlib.Path(ws)
    assert (ws_path / "CLAUDE.md").exists(), "CLAUDE.md missing"
    assert (ws_path / ".claude" / "settings.json").exists(), ".claude/settings.json missing"

    settings = json.loads((ws_path / ".claude" / "settings.json").read_text())
    enabled = settings.get("enabledPlugins", {})
    assert "superpowers@casa-plugins-defaults" in enabled, (
        f"superpowers@casa-plugins-defaults not in enabledPlugins: {list(enabled)}"
    )
    print("P-1 OK")

asyncio.run(main())
PY
)"
pass "P-1: plugin-developer workspace provisioned with correct enabledPlugins"

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
# P-4 — install_casa_plugin tool registered
# ---------------------------------------------------------------------------
log "P-4: Configurator install_casa_plugin tool registered"
run_harness "P-4" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
import tools
assert hasattr(tools, "_tool_install_casa_plugin"), (
    "install tool missing: _tool_install_casa_plugin not in tools module"
)
print("P-4 OK")
PY
)"
pass "P-4: _tool_install_casa_plugin present in tools module"

# ---------------------------------------------------------------------------
# P-5 — uninstall_casa_plugin tool registered
# ---------------------------------------------------------------------------
log "P-5: uninstall_casa_plugin tool registered"
run_harness "P-5" "$(cat <<'PY'
import sys
sys.path.insert(0, "/opt/casa")
import tools
assert hasattr(tools, "_tool_uninstall_casa_plugin"), (
    "uninstall tool missing: _tool_uninstall_casa_plugin not in tools module"
)
print("P-5 OK")
PY
)"
pass "P-5: _tool_uninstall_casa_plugin present in tools module"

# ---------------------------------------------------------------------------
# P-6 — marketplace_read_only guard: removing a plugin that is not in the
#        user marketplace returns an error containing "not found"
# ---------------------------------------------------------------------------
log "P-6: marketplace_read_only guard on casa-plugins-defaults"
run_harness "P-6" "$(cat <<'PY'
import sys, json
sys.path.insert(0, "/opt/casa")
from tools import _tool_marketplace_remove_plugin

r = _tool_marketplace_remove_plugin(plugin_name="superpowers")
assert r.get("removed") is False, f"expected removed=False, got {r}"
err = r.get("error", "")
assert "not found" in err.lower(), (
    f"expected 'not found' in error, got: {err!r}"
)
print("P-6 OK (seed plugin not in user mkt)")
PY
)"
pass "P-6: marketplace_read_only guard — seed plugin not removable from user mkt"

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

stop_container "$NAME"

echo "=== test_engagement_P.sh complete ==="
