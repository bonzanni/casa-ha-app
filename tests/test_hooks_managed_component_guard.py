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


# ------------------------------------------------------------------
# Round-2 F2: RELATIVE paths (executor cwd is /config) resolve against
# /config before the prefix test — Write/Edit file_path and Bash tokens
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
@pytest.mark.parametrize("path,route_marker", [
    ("agents/specialists/x/runtime.yaml", "specialist_install_inspect"),
    ("specialists/x/0.1.0/spec.yaml", "specialist_install_inspect"),
    ("./bindings/ellen.yaml", "resident_persona_swap"),
    ("plugins/registry.json", "plugin_add"),
])
async def test_relative_write_edit_denied(tool_name, path, route_marker):
    out = await _hook()(
        {"tool_name": tool_name, "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    assert route_marker in _reason(out), path


async def test_relative_hooks_yaml_write_denied():
    out = await _hook()(
        {"tool_name": "Edit",
         "tool_input": {"file_path": "agents/executors/configurator/hooks.yaml"}},
        None, {},
    )
    assert _decision(out) == "deny"
    assert "hook-policy file" in _reason(out)


async def test_relative_write_outside_managed_allowed():
    out = await _hook()(
        {"tool_name": "Write", "tool_input": {"file_path": "workspace/notes.md"}},
        None, {},
    )
    assert out == {}


@pytest.mark.parametrize("cmd,route_marker", [
    ("echo 'name: x' > agents/specialists/x/runtime.yaml",
     "specialist_install_inspect"),
    ("mkdir -p specialists/newbot", "specialist_install_inspect"),
    ("echo x > ./bindings/gary.yaml", "resident_persona_swap"),
    # cwd=/ spelling resolves via the "/"-join candidate.
    ("echo x > config/plugins/registry.json", "plugin_add"),
    # Bare managed-root word: `rm -rf plugins` from /config kills the store.
    ("rm -rf plugins", "plugin_add"),
])
async def test_relative_bash_write_forms_denied(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


async def test_relative_bash_read_passes():
    out = await _hook()(
        {"tool_name": "Bash",
         "tool_input": {"command": "cat agents/specialists/finance/runtime.yaml"}},
        None, {},
    )
    assert out == {}


# ------------------------------------------------------------------
# Round-2 F1: Bash write-forms against hooks.yaml under /config/agents/
# deny with the hook-policy message (self-disarm was the P0 bypass)
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "sed -i 's/x/y/' /config/agents/executors/configurator/hooks.yaml",
    "sed -i 's/x/y/' agents/executors/configurator/hooks.yaml",
    "echo '' > /config/agents/ellen/hooks.yaml",
    "rm agents/executors/plugin-developer/hooks.yaml",
])
async def test_bash_hooks_yaml_write_denied(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert "hook-policy file" in _reason(out), cmd


async def test_bash_hooks_yaml_read_passes():
    out = await _hook()(
        {"tool_name": "Bash",
         "tool_input": {
             "command": "cat /config/agents/executors/configurator/hooks.yaml"}},
        None, {},
    )
    assert out == {}


# ------------------------------------------------------------------
# Round-2 F3: symlink resolution. Strongest testable form: full hook
# invocation over a REAL on-disk symlink in tmp_path, with only the
# managed-prefix table repointed at the tmp layout (monkeypatched
# _MANAGED_PREFIX_ROUTES — _managed_prefix_route reads the module global
# at call time, so the production realpath+lexical composition runs
# unmodified end to end).
# ------------------------------------------------------------------


class TestSymlinkResolution:
    def _layout(self, tmp_path, monkeypatch):
        import hooks as hooks_mod
        managed = tmp_path / "config" / "plugins"
        managed.mkdir(parents=True)
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "link").symlink_to(managed, target_is_directory=True)
        monkeypatch.setattr(
            hooks_mod, "_MANAGED_PREFIX_ROUTES",
            ((str(managed), hooks_mod._MANAGED_ROUTE_PLUGINS),))
        return ws

    async def test_write_through_symlink_denied(self, tmp_path, monkeypatch):
        ws = self._layout(tmp_path, monkeypatch)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(ws / "link" / "evil.json")}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "plugin_add" in _reason(out)

    async def test_bash_through_symlink_denied(self, tmp_path, monkeypatch):
        ws = self._layout(tmp_path, monkeypatch)
        out = await _hook()(
            {"tool_name": "Bash",
             "tool_input": {"command": f"echo x > {ws}/link/evil.json"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "plugin_add" in _reason(out)

    async def test_write_beside_symlink_allowed(self, tmp_path, monkeypatch):
        ws = self._layout(tmp_path, monkeypatch)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(ws / "direct.json")}},
            None, {},
        )
        assert out == {}


# ------------------------------------------------------------------
# Round-2 F3 fail-closed: OSError during realpath resolution denies
# ------------------------------------------------------------------


class TestRealpathOSErrorFailsClosed:
    def _patch(self, monkeypatch):
        import hooks as hooks_mod
        real = hooks_mod.os.path.realpath

        def _boom(path, *args, **kwargs):
            # Conditional on a sentinel so incidental realpath users
            # elsewhere in the process are unaffected.
            if "sentinel-oserr" in str(path):
                raise OSError("synthetic realpath failure")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(hooks_mod.os.path, "realpath", _boom)

    async def test_write_denies(self, monkeypatch):
        self._patch(monkeypatch)
        out = await _hook()(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/tmp/sentinel-oserr/x.yaml"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)

    async def test_bash_denies(self, monkeypatch):
        self._patch(monkeypatch)
        out = await _hook()(
            {"tool_name": "Bash",
             "tool_input": {"command": "echo x > /tmp/sentinel-oserr/f"}},
            None, {},
        )
        assert _decision(out) == "deny"
        assert "failing closed" in _reason(out)


# ------------------------------------------------------------------
# Round-2 F4: inline-interpreter escape denies when a managed token
# appears anywhere in the command, regardless of write verbs
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd,route_marker", [
    ("python3 -c \"open('/config/plugins/registry.json','w').write('x')\"",
     "plugin_add"),
    ("python3 -c \"open('agents/specialists/x/runtime.yaml','w')\"",
     "specialist_install_inspect"),
    ("perl -e 'unlink q{/config/bindings/ellen.yaml}'",
     "resident_persona_swap"),
    ("node --eval \"require('fs').rmSync('/config/specialists/x',"
     "{recursive:true})\"", "specialist_install_inspect"),
    # Wrapped invocation still detected.
    ("bash -c 'python3 -c \"open(\\\"plugins/registry.json\\\",\\\"w\\\")\"'",
     "plugin_add"),
])
async def test_inline_interpreter_with_managed_token_denied(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


@pytest.mark.parametrize("cmd", [
    # No managed token — inline code alone is not a deny.
    "python3 -c \"print('hello world')\"",
    # Managed token but NO inline-code flag and no write shape (plain
    # script execution / read) stays allowed.
    "python3 /config/plugins/store/x/main.py",
])
async def test_interpreter_forms_without_both_conditions_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


# ------------------------------------------------------------------
# Round-2 F5: write VERBS only count in command position — verbs inside
# argument text (grep patterns/args) no longer false-deny
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "grep -r install /config/plugins/store",
    "grep tar /config/plugins/x",
    "grep -r 'touch' /config/bindings",
    "git -C /config log -- agents/specialists",
])
async def test_write_verbs_in_argument_text_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


@pytest.mark.parametrize("cmd,route_marker", [
    ("install -m 644 f /config/plugins/x", "plugin_add"),
    ("find /config/specialists -delete", "specialist_install_inspect"),
    # Wrapper prefixes still expose the command-position verb.
    ("sudo cp /tmp/x /config/bindings/ellen.yaml", "resident_persona_swap"),
    ("timeout 5 rm -rf /config/specialists/x", "specialist_install_inspect"),
    ("find /tmp -name '*.yaml' | xargs cp -t /config/bindings",
     "resident_persona_swap"),
])
async def test_command_position_verbs_still_deny(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd
