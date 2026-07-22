"""Tests for managed_component_guard hook policy (#210, v0.101.0).

On a fresh install the configurator hand-authored specialist files under
/config/agents/specialists/ via Bash instead of the typed
specialist_install_* pipeline; its path_scope makes /config/agents writable
and only /config/plugins/ Bash-writes were specially denied. The policy
denies hand-edits (Write/Edit by normalized path, Bash by write-shaped
commands) of the managed component trees and routes the model to the typed
tool for the matched tree; reads stay allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_EXECUTORS_DIR = (
    Path(__file__).resolve().parents[1]
    / "casa-agent/rootfs/opt/casa/defaults/agents/executors"
)


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _hook():
    from hooks import make_managed_component_guard
    return make_managed_component_guard()


# ------------------------------------------------------------------
# Write/Edit deny under each managed prefix (incl. traversal and //)
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
@pytest.mark.parametrize("path,route_marker", [
    ("/config/agents/specialists/finance/runtime.yaml",
     "specialist_install_inspect"),
    ("/config/specialists/finance/0.1.0/spec.yaml",
     "specialist_install_inspect"),
    ("/config/bindings/ellen.yaml", "resident_persona_swap"),
    ("/config/plugins/registry.json", "plugin_add"),
    # `..` traversal and `//` collapse must resolve to the managed prefix
    # (same normalization contract as path_scope / casa_config_guard).
    ("/config/agents/other/../specialists/finance/runtime.yaml",
     "specialist_install_inspect"),
    ("//config//plugins/registry.json", "plugin_add"),
    ("/config/policies/../bindings/ellen.yaml", "resident_persona_swap"),
])
async def test_write_edit_denied_under_managed_prefix(
        tool_name, path, route_marker):
    out = await _hook()(
        {"tool_name": tool_name, "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    # The deny reason must ROUTE the model to the typed pipeline.
    assert route_marker in _reason(out), path
    assert "hand-editing is forbidden" in _reason(out)


@pytest.mark.parametrize("path", [
    # NOT managed: ordinary resident config the configurator legitimately edits.
    "/config/agents/assistant/triggers.yaml",
    "/config/policies/x.yaml",
    "/config/workspace/scratch.md",
    # hooks.yaml OUTSIDE /config/agents/ is not a policy-file self-edit.
    "/config/hooks.yaml",
])
async def test_write_allowed_outside_managed_prefixes(path):
    out = await _hook()(
        {"tool_name": "Write", "tool_input": {"file_path": path}},
        None, {},
    )
    assert out == {}, path


# ------------------------------------------------------------------
# hooks.yaml policy-file self-editing (any agent under /config/agents/)
# ------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/config/agents/ellen/hooks.yaml",
    "/config/agents/executors/configurator/hooks.yaml",
    "/config/agents/executors/plugin-developer/hooks.yaml",
    "/config/agents/ellen/../ellen/hooks.yaml",  # traversal spelling
])
async def test_hooks_yaml_self_edit_denied(path):
    out = await _hook()(
        {"tool_name": "Edit", "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    assert "hook-policy file" in _reason(out)


# ------------------------------------------------------------------
# Bash write-forms into managed prefixes deny
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd,route_marker", [
    ("echo 'name: x' > /config/agents/specialists/x/runtime.yaml",
     "specialist_install_inspect"),
    ("cat /tmp/f | tee /config/specialists/x/spec.yaml", "specialist_install_inspect"),
    ("cp /tmp/x /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("mv /tmp/x /config/plugins/registry.json", "plugin_add"),
    ("mkdir -p /config/agents/specialists/newbot", "specialist_install_inspect"),
    ("rm -rf /config/specialists/finance", "specialist_install_inspect"),
    ("sed -i 's/a/b/' /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("ln -s /tmp/tgt /config/plugins/store/x", "plugin_add"),
    # Heredoc into a managed path.
    ("cat > /config/agents/specialists/x/agent.yaml <<'EOF'\nname: x\nEOF",
     "specialist_install_inspect"),
    # bash -c wrapper: the raw string still carries the path token, so no
    # argv unwrapping is needed to catch it.
    ("bash -c 'echo x > /config/plugins/registry.json'", "plugin_add"),
    # Traversal / double-slash spellings resolve to the managed prefix.
    ("echo x > /config/agents/specialists/../../plugins/registry.json",
     "plugin_add"),
    ("echo x > //config//plugins/registry.json", "plugin_add"),
    ("touch /config/bindings/gary.yaml", "resident_persona_swap"),
    ("rsync -a /tmp/stage/ /config/specialists/x/", "specialist_install_inspect"),
    ("chmod 755 /config/agents/specialists/x/run.sh", "specialist_install_inspect"),
    ("git checkout -- /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("tar -xf /tmp/a.tar -C /config/specialists/", "specialist_install_inspect"),
    ("find /config/plugins/store -name '*.pyc' -delete", "plugin_add"),
])
async def test_bash_write_forms_denied(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


# ------------------------------------------------------------------
# Bash read-forms pass (the configurator legitimately READS these trees)
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "cat /config/agents/specialists/finance/runtime.yaml",
    "ls -la /config/specialists",
    "grep -r persona /config/bindings",
    "git -C /config log --oneline -5",
    "git -C /config status",
    "git -C /config diff",
    "find /config/plugins/store -name plugin.json",
    # Benign stderr redirects must not read as writes.
    "cat /config/plugins/store/x/plugin.json 2>/dev/null",
    "ls /config/bindings 2>&1",
    # No managed prefix mentioned at all.
    "echo hello > /tmp/scratch.txt",
])
async def test_bash_read_forms_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


# ------------------------------------------------------------------
# Fail-closed: an internal exception returns the deny shape, never escapes
# ------------------------------------------------------------------


class TestFailClosed:
    async def test_write_internal_error_denies(self, monkeypatch):
        import hooks as hooks_mod

        def _boom(norm):
            raise RuntimeError("synthetic internal failure")

        monkeypatch.setattr(hooks_mod, "_managed_prefix_route", _boom)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path":
                 "/config/agents/specialists/x/runtime.yaml"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)

    async def test_bash_internal_error_denies(self, monkeypatch):
        import hooks as hooks_mod

        def _boom(command):
            raise RuntimeError("synthetic internal failure")

        monkeypatch.setattr(hooks_mod, "_bash_managed_prefix_route", _boom)
        out = await _hook()(
            {"tool_name": "Bash",
             "tool_input": {"command": "echo x > /config/plugins/r.json"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)


# ------------------------------------------------------------------
# No-op paths return {} exactly (H-2 contract — mirror
# TestHookNoopReturnsEmptyDict in test_hooks_policies.py)
# ------------------------------------------------------------------


class TestNoopReturnsEmptyDict:
    async def test_non_matching_tool(self):
        out = await _hook()(
            {"tool_name": "Read",
             "tool_input": {"file_path": "/config/plugins/registry.json"}},
            None, {},
        )
        assert out == {}, f"expected empty dict, got {out!r}"

    async def test_allowed_write(self):
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/config/agents/assistant/triggers.yaml"}},
            None, {},
        )
        assert out == {}, f"expected empty dict, got {out!r}"

    async def test_safe_bash(self):
        out = await _hook()(
            {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}},
            None, {},
        )
        assert out == {}, f"expected empty dict, got {out!r}"


# ------------------------------------------------------------------
# Registry shape + factory conventions
# ------------------------------------------------------------------


async def test_registered_in_hook_policies():
    from hooks import HOOK_POLICIES
    assert "managed_component_guard" in HOOK_POLICIES
    assert HOOK_POLICIES["managed_component_guard"]["matcher"] == "Write|Edit|Bash"


async def test_factory_returns_hookcallback():
    import inspect
    from hooks import HOOK_POLICIES
    cb = HOOK_POLICIES["managed_component_guard"]["factory"]()
    assert inspect.iscoroutinefunction(cb)


async def test_factory_rejects_params():
    from hooks import HOOK_POLICIES, UnknownPolicyError
    with pytest.raises(UnknownPolicyError, match="bogus"):
        HOOK_POLICIES["managed_component_guard"]["factory"](bogus=True)


# ------------------------------------------------------------------
# Shipped hooks.yaml wiring — the policy must be active through the
# resolved stacks of BOTH executors (pattern:
# tests/test_hooks_resolve_executor_params.py)
# ------------------------------------------------------------------


ENG_ID = "a" * 32


class _Rec:
    status = "active"
    role_or_type = "configurator"


class _Registry:
    def get(self, eng_id):
        return _Rec() if eng_id == ENG_ID else None


def _shipped_hooks_yaml(executor: str) -> dict:
    return yaml.safe_load(
        (_EXECUTORS_DIR / executor / "hooks.yaml").read_text(encoding="utf-8"))


def _configurator_handler():
    from casa_core import _build_cc_hook_policies
    from hooks import HOOK_POLICIES, build_policy_callbacks_from_hooks_yaml
    from internal_handlers import _make_internal_hooks_resolve_handler
    return _make_internal_hooks_resolve_handler(
        hook_policies=_build_cc_hook_policies(HOOK_POLICIES),
        executor_hook_policies={
            "configurator": build_policy_callbacks_from_hooks_yaml(
                _shipped_hooks_yaml("configurator")
            )},
        engagement_registry=_Registry(),
    )


async def test_shipped_configurator_stack_denies_specialist_write():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _configurator_handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "managed_component_guard",
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {
                            "file_path":
                                "/config/agents/specialists/x/runtime.yaml"}}})
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "specialist_install_inspect" in (
            body["hookSpecificOutput"]["permissionDecisionReason"])


async def test_shipped_configurator_stack_allows_resident_write():
    app = web.Application()
    app.router.add_post("/hooks/resolve", _configurator_handler())
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post("/hooks/resolve", json={
            "policy": "managed_component_guard",
            "payload": {"tool_name": "Write",
                        "cwd": f"/data/engagements/{ENG_ID}",
                        "tool_input": {
                            "file_path":
                                "/config/agents/assistant/triggers.yaml"}}})
        assert await resp.json() == {}


async def test_shipped_yaml_declares_policy_for_both_executors():
    """Both shipped executor hooks.yaml files carry the policy, and
    build_policy_callbacks_from_hooks_yaml materializes it (HTTP hook path)."""
    from hooks import build_policy_callbacks_from_hooks_yaml
    for executor in ("configurator", "plugin-developer"):
        data = _shipped_hooks_yaml(executor)
        declared = [e.get("policy") for e in data["pre_tool_use"]]
        assert "managed_component_guard" in declared, executor
        built = build_policy_callbacks_from_hooks_yaml(data)
        assert "managed_component_guard" in built, executor
        matcher, _cb = built["managed_component_guard"]
        assert matcher == "Write|Edit|Bash"


async def test_shipped_configurator_yaml_resolves_on_sdk_path():
    """resolve_hooks (SDK path) accepts the shipped configurator hooks.yaml
    and the resolved stack denies a specialist hand-write."""
    from config import HooksConfig
    from hooks import resolve_hooks

    data = _shipped_hooks_yaml("configurator")
    resolved = resolve_hooks(
        HooksConfig(pre_tool_use=data["pre_tool_use"]), default_cwd="/config")
    payload = {"tool_name": "Write",
               "tool_input": {"file_path":
                   "/config/agents/specialists/x/runtime.yaml"}}
    reasons = []
    for matcher in resolved["PreToolUse"]:
        for cb in matcher.hooks:
            out = await cb(payload, None, {})
            if out and _decision(out) == "deny":
                reasons.append(_reason(out))
    assert any("specialist_install_inspect" in r for r in reasons), reasons
