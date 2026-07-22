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


# ------------------------------------------------------------------
# Round-2 W2 (Sol): /config/personas is managed — persona installs are
# consent-gated exactly like specialists
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
@pytest.mark.parametrize("path", [
    "/config/personas/warm-helper/0.1.0/persona.yaml",
    "personas/warm-helper/0.1.0/persona.yaml",   # relative (F2 applies too)
])
async def test_personas_write_edit_denied(tool_name, path):
    out = await _hook()(
        {"tool_name": tool_name, "tool_input": {"file_path": path}},
        None, {},
    )
    assert _decision(out) == "deny", path
    assert "persona_install_inspect" in _reason(out), path


@pytest.mark.parametrize("cmd", [
    "cp /tmp/p.yaml /config/personas/warm-helper/0.1.0/persona.yaml",
    "echo x > personas/warm-helper/0.1.0/persona.yaml",
    "rm -rf /config/personas/warm-helper",
])
async def test_personas_bash_write_denied(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert "persona_install_inspect" in _reason(out), cmd


async def test_personas_bash_read_passes():
    out = await _hook()(
        {"tool_name": "Bash",
         "tool_input": {"command": "ls -la /config/personas"}}, None, {},
    )
    assert out == {}


# ------------------------------------------------------------------
# Round-2 W3 (Sol): target-aware redirects and cp/rsync/install
# destinations — reading FROM a managed tree is not a write
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    # Copy FROM managed = read of managed, write elsewhere.
    "cp /config/plugins/store/x/file /tmp/out",
    # stderr redirect to a NON-managed target is not a managed write.
    "grep pat /config/plugins/store/x 2>/tmp/error",
    "grep pat /config/specialists/x 2>&1 | head",
])
async def test_managed_source_read_shapes_pass(cmd):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert out == {}, cmd


# ------------------------------------------------------------------
# Round-4 (Terra P0): managed_component_guard is CODE-MANDATORY for
# executor sessions. definition.yaml's `hooks_file:` is a config-editable
# pointer (executor/edit-definition is a legitimate recipe); repointing it
# at a hollow yaml must not shed the guard on the next session. Covered on
# BOTH drivers: in_casa (tools._build_executor_options) and claude_code
# (drivers.hook_bridge.translate_hooks_to_settings +
# hooks.build_policy_callbacks_from_hooks_yaml).
# ------------------------------------------------------------------


_SPECIALIST_WRITE = {
    "tool_name": "Write",
    "tool_input": {"file_path": "/config/agents/specialists/x/runtime.yaml"},
}


async def _stack_denies_managed(opts) -> bool:
    """Invoke every PreToolUse callback in built ClaudeAgentOptions against
    a specialist hand-write; True iff any returns a managed deny."""
    for matcher in opts.hooks["PreToolUse"]:
        for cb in matcher.hooks:
            out = await cb(dict(_SPECIALIST_WRITE), None, {})
            if out and _decision(out) == "deny" \
                    and "managed_component_guard" in _reason(out):
                return True
    return False


class TestGuardIsCodeMandatoryInCasa:
    def _defn(self, hooks_path):
        from types import SimpleNamespace
        return SimpleNamespace(
            hooks_path=hooks_path, mcp_server_names=[], tools_allowed=["Read"],
            model="sonnet", permission_mode="acceptEdits",
            tools_disallowed=[], driver="in_casa",
        )

    def _build(self, hooks_path):
        import tools
        return tools._build_executor_options(
            self._defn(hooks_path), executor_type="configurator",
            plugin_paths=[],
        )

    async def test_yaml_without_guard_still_denies(self, tmp_path):
        """(a) hooks.yaml exists but does NOT declare the policy."""
        p = tmp_path / "hooks.yaml"
        p.write_text(
            "pre_tool_use:\n"
            "  - policy: path_scope\n"
            "    writable: [/config]\n"
            "    readable: [/config]\n",
            encoding="utf-8")
        assert await _stack_denies_managed(self._build(str(p)))

    async def test_hollow_hooks_yaml_still_denies(self, tmp_path):
        """(b) the Terra regression: hooks_file repointed at a hollow yaml."""
        p = tmp_path / "hollow.yaml"
        p.write_text("pre_tool_use: []\n", encoding="utf-8")
        assert await _stack_denies_managed(self._build(str(p)))

    async def test_missing_hooks_file_still_denies(self):
        """(b') hooks_file pointing nowhere (defn.hooks_path unusable)."""
        assert await _stack_denies_managed(self._build(None))

    async def test_declared_guard_not_double_appended(self):
        """(3) The shipped yaml declares the policy — the code-side append
        dedupes on the declared name, so the stack is yaml entries + the
        settings guard only (and still denies)."""
        import yaml as yaml_mod
        shipped = _EXECUTORS_DIR / "configurator" / "hooks.yaml"
        n_declared = len(
            yaml_mod.safe_load(shipped.read_text(encoding="utf-8"))
            ["pre_tool_use"])
        opts = self._build(str(shipped))
        # + 1 = agent_home_settings_guard matcher; no second managed guard.
        assert len(opts.hooks["PreToolUse"]) == n_declared + 1
        assert await _stack_denies_managed(opts)


class TestGuardIsCodeMandatoryClaudeCode:
    async def test_hollow_yaml_settings_carry_guard_entry(self):
        """(c) CC settings emission: a hollow hooks.yaml still emits the
        hook_proxy.sh managed_component_guard PreToolUse entry."""
        from drivers.hook_bridge import translate_hooks_to_settings
        settings = translate_hooks_to_settings(
            {}, proxy_script_path="/opt/casa/scripts/hook_proxy.sh")
        pre = settings["hooks"]["PreToolUse"]
        cmds = [h["command"] for e in pre for h in e["hooks"]]
        assert "/opt/casa/scripts/hook_proxy.sh managed_component_guard" in cmds

    async def test_shipped_yaml_settings_not_double_emitted(self):
        """(3) Shipped plugin-developer yaml declares the policy (bare ->
        matcher '.*' covers) — exactly one guard entry emitted."""
        import yaml as yaml_mod
        from drivers.hook_bridge import translate_hooks_to_settings
        for executor in ("configurator", "plugin-developer"):
            raw = yaml_mod.safe_load(
                (_EXECUTORS_DIR / executor / "hooks.yaml")
                .read_text(encoding="utf-8"))
            settings = translate_hooks_to_settings(
                raw, proxy_script_path="/opt/casa/scripts/hook_proxy.sh")
            cmds = [h["command"]
                    for e in settings["hooks"]["PreToolUse"]
                    for h in e["hooks"]]
            guard = [c for c in cmds if c.endswith("managed_component_guard")]
            assert len(guard) == 1, (executor, cmds)

    async def test_narrowed_matcher_declaration_does_not_satisfy_mandate(self):
        """A yaml-declared guard with a NARROW matcher (matchers are
        attacker-editable on the CC path) does not suppress the canonical
        appended entry."""
        from drivers.hook_bridge import translate_hooks_to_settings
        settings = translate_hooks_to_settings(
            {"pre_tool_use": [
                {"policy": "managed_component_guard", "matcher": "Read"}]},
            proxy_script_path="/opt/casa/scripts/hook_proxy.sh")
        pre = settings["hooks"]["PreToolUse"]
        canonical = [e for e in pre if e["matcher"] == "Write|Edit|Bash"]
        assert len(canonical) == 1

    async def test_resolved_policy_set_always_carries_guard(self):
        """(2) Server side: build_policy_callbacks_from_hooks_yaml injects
        the guard into the resolved set regardless of yaml contents, and
        the callback denies a specialist hand-write."""
        from hooks import build_policy_callbacks_from_hooks_yaml
        built = build_policy_callbacks_from_hooks_yaml({})
        assert "managed_component_guard" in built
        matcher, cb = built["managed_component_guard"]
        assert matcher == "Write|Edit|Bash"
        out = await cb(dict(_SPECIALIST_WRITE), None, {})
        assert _decision(out) == "deny"


@pytest.mark.parametrize("cmd,route_marker", [
    ("cp /tmp/x /config/plugins/store/y", "plugin_add"),
    ("cp /config/specialists/a /config/specialists/b",
     "specialist_install_inspect"),
    ("echo x > /config/specialists/f", "specialist_install_inspect"),
    # mv mutates its SOURCE too (removes it) — stays blanket, not
    # destination-only (deviation from the literal W3 family list; see
    # the _MANAGED_BLANKET_WRITE_VERBS comment).
    ("mv /config/plugins/store/x /tmp/", "plugin_add"),
    # Option-terminated operand shape is ambiguous -> any managed operand
    # denies (fail-closed).
    ("cp /config/plugins/store/x /tmp/y -S .bak", "plugin_add"),
    # Runtime-expanded redirect target is unknowable -> ambiguous -> deny.
    ("cat /config/bindings/ellen.yaml > $OUT", "resident_persona_swap"),
])
async def test_destination_aware_write_shapes_deny(cmd, route_marker):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {},
    )
    assert _decision(out) == "deny", cmd
    assert route_marker in _reason(out), cmd


# ------------------------------------------------------------------
# Round-3 S2: GNU attached-form `-tDIR` target directory (cp/install)
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "cp -t/config/plugins /tmp/x",
    "install -t/config/agents/specialists/finance /tmp/payload",
    "cp -t/config/personas/pack /tmp/manifest.yaml",
])
async def test_bash_attached_target_directory_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


async def test_bash_rsync_times_cluster_does_not_downgrade_dest_check():
    # rsync's -t is preserve-times, NOT a target directory: a cluster like
    # `-tv` must not be misread as target "v" (which would turn the managed
    # last operand into an ignored "source").
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "rsync -tv /tmp/evil /config/plugins/registry.json"}},
        None, {})
    assert _decision(out) == "deny"


async def test_bash_attached_target_nonmanaged_copy_still_reads_ok():
    # Attached -t pointing OUTSIDE managed trees with a managed SOURCE:
    # single-operand shape is ambiguous -> fail-closed deny is accepted; the
    # unambiguous separate-form read (two operands, non-managed dest) passes.
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "cp /config/plugins/store/x/manifest.json /tmp/out.json"}},
        None, {})
    assert out == {}


# ------------------------------------------------------------------
# Round-3 S4: bindings route names the FULL lifecycle set
# ------------------------------------------------------------------


async def test_bindings_route_names_all_lifecycle_tools():
    out = await _hook()(
        {"tool_name": "Write",
         "tool_input": {"file_path": "/config/bindings/ellen.yaml"}},
        None, {},
    )
    for marker in ("resident_persona_swap", "resident_persona_reset",
                   "persona_apply"):
        assert marker in _reason(out), marker


# ------------------------------------------------------------------
# Round-7 (Terra): git subcommands allowlisted read-only, not denylisted
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git -C /config/agents/specialists apply /tmp/patch",
    # NOTE: a `git apply` whose -C target is a NON-managed parent (e.g.
    # /config or the executors dir) with the mutation hidden in the patch
    # CONTENT is the opaque-content residual (same class as script files /
    # stdin) — closed for the configurator at the capability layer (no
    # shell), tracked for plugin-developer under #216.
    "git -C /config/specialists am /tmp/series.mbox",
    "git -C /config/plugins stash pop",
    "git -C /config/bindings mv a.yaml b.yaml",
    "git -C /config/personas rm -r pack",
    "git -C /config/agents/specialists checkout -- finance/runtime.yaml",
])
async def test_bash_git_mutating_subcommands_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


@pytest.mark.parametrize("command", [
    "git -C /config log --oneline -5",
    "git -C /config/agents/specialists status",
    "git -C /config diff HEAD~1 -- agents/specialists/finance/runtime.yaml",
    "git -C /config show HEAD:agents/specialists/finance/runtime.yaml",
    "git -C /config/plugins ls-files",
])
async def test_bash_git_readonly_subcommands_pass(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert out == {}, command


async def test_bash_git_unknown_subcommand_fails_closed():
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git -C /config/specialists filter-branch --force"}},
        None, {})
    assert _decision(out) == "deny"


# ------------------------------------------------------------------
# Round-8 (Sol): --output on read-only git subcommands is a write
# ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git -C /config log --output=/config/plugins/stolen.txt",
    "git -C /config show HEAD --output /config/agents/specialists/finance/x",
    "git -C /config diff --output=$OUT agents/specialists/finance/runtime.yaml",
    "git log --output /config/plugins/exfil.json",
])
async def test_bash_git_output_option_write_denied(command):
    out = await _hook()(
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, {})
    assert _decision(out) == "deny", command


async def test_bash_git_output_to_nonmanaged_from_managed_read_passes():
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git -C /config log --output=/tmp/export.txt -- "
                   "agents/specialists/finance/runtime.yaml"}}, None, {})
    assert out == {}


async def test_bash_git_config_injection_pre_subcommand_denied():
    # Round-8 (Terra): -c/--paginate/--exec-path before the subcommand can
    # execute arbitrary commands with the payload argv-visible.
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git -c core.pager='sh -c \"touch /config/plugins/x\"' "
                   "--paginate log"}}, None, {})
    assert _decision(out) == "deny"


async def test_bash_git_no_pager_read_still_passes():
    out = await _hook()({"tool_name": "Bash", "tool_input": {
        "command": "git --no-pager -C /config log --oneline -- "
                   "agents/specialists/finance/runtime.yaml"}}, None, {})
    assert out == {}
