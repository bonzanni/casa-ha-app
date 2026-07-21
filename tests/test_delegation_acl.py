"""Tests for the delegate_to_agent delegation ACL (spec A1)."""
from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from config import AgentConfig, CharacterConfig, DelegateEntry, ToolsConfig

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _cfg(role, delegates=()):
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.character = CharacterConfig(name=role.title(), archetype=role, card="", prompt=f"You are {role}.")
    cfg.enabled = True; cfg.model = "haiku"
    cfg.tools = ToolsConfig(allowed=["Read"], disallowed=[], permission_mode="acceptEdits", max_turns=5)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    cfg.system_prompt = f"You are {role}."
    return cfg


def _full_specialist_reg():
    reg = MagicMock()
    reg.get.return_value = None
    reg.register_delegation = AsyncMock()
    reg.cancel_delegation = AsyncMock()
    reg.fail_delegation = AsyncMock()
    reg.complete_delegation = AsyncMock()
    return reg


def _init(agent_role_map):
    from tools import init_tools
    init_tools(channel_manager=MagicMock(), bus=MagicMock(),
               specialist_registry=_full_specialist_reg(), mcp_registry=MagicMock(),
               trigger_registry=MagicMock(), engagement_registry=MagicMock(),
               agent_role_map=agent_role_map)


async def _call(target, *, role="", execution_role=None, mode="sync",
                monkeypatch=None, extra_origin=None, extra_args=None):
    """Invoke delegate_to_agent with a synthetic origin.

    Caller identity is set ONLY via origin (role / execution_role) — the
    handler must never read identity from tool args. ``extra_args`` lets a
    test plant decoy role-like fields in the args to prove they're ignored.
    """
    import agent as agent_mod
    from tools import delegate_to_agent
    if monkeypatch is not None:
        import tools as tm
        async def _never_run(*a, **k):  # guard: ACL denials must not launch
            raise AssertionError("delegated runner reached despite ACL")
        monkeypatch.setattr(tm, "_run_delegated_agent", _never_run)
    origin = {"role": role, "channel": "telegram",
              "chat_id": "c1", "cid": "t", "user_text": "hi"}
    if execution_role is not None:
        origin["execution_role"] = execution_role
    if extra_origin:
        origin.update(extra_origin)
    args = {"agent": target, "task": "t", "context": "", "mode": mode}
    if extra_args:
        args.update(extra_args)
    token = agent_mod.origin_var.set(origin)
    try:
        res = await delegate_to_agent.handler(args)
    finally:
        agent_mod.origin_var.reset(token)
    return json.loads(res["content"][0]["text"])


class TestDelegationACL:
    async def test_undeclared_target_denied(self, monkeypatch):
        _init({"concierge": _cfg("concierge", ["mtg"]), "assistant": _cfg("assistant", ["finance"])})
        p = await _call("assistant", role="concierge", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"

    async def test_undeclared_absent_target_denied_before_lookup(self, monkeypatch):
        # The undeclared target is ABSENT from the role map. An impl that
        # resolved the target before the ACL would still deny (unknown_agent)
        # — so we ALSO assert the registry fallback was never consulted,
        # proving the ACL fires strictly before any lookup.
        import tools as tm
        _init({"concierge": _cfg("concierge", ["mtg"])})
        p = await _call("ghosttarget", role="concierge", monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"
        tm._specialist_registry.get.assert_not_called()

    async def test_unknown_caller_denied(self, monkeypatch):
        _init({"assistant": _cfg("assistant", ["finance"])})
        assert (await _call("assistant", role="ghost", monkeypatch=monkeypatch))["kind"] == "delegation_not_declared"

    async def test_missing_caller_role_denied(self, monkeypatch):
        _init({"assistant": _cfg("assistant", ["finance"])})
        assert (await _call("assistant", role="", monkeypatch=monkeypatch))["kind"] == "delegation_not_declared"

    async def test_missing_role_at_depth1_denied_not_depth_exceeded(self, monkeypatch):
        # A role-less caller ALREADY at depth 1: the ACL must fire before the
        # depth-cap branch, so the denial is delegation_not_declared (caller
        # identity), NOT delegation_depth_exceeded.
        _init({"assistant": _cfg("assistant", ["finance"])})
        p = await _call("assistant", role="", monkeypatch=monkeypatch,
                        extra_origin={"delegation_depth": 1})
        assert p["kind"] == "delegation_not_declared"

    async def test_declared_but_absent_target_reaches_lookup(self):
        _init({"assistant": _cfg("assistant", ["finance"])})
        assert (await _call("finance", role="assistant"))["kind"] == "unknown_agent"

    async def test_reload_synced_map_enforced(self, monkeypatch):
        # After sync_agent_role_map rebuilds the map, the ACL uses the NEW
        # delegates (forged/removed declarations take effect immediately).
        import tools as tm
        _init({"assistant": _cfg("assistant", ["finance"])})
        runtime = MagicMock()
        runtime.role_configs = {"assistant": _cfg("assistant", [])}  # finance removed
        runtime.specialist_registry = MagicMock(all_configs=MagicMock(return_value={}))
        tm.sync_agent_role_map(runtime)
        assert (await _call("finance", role="assistant", monkeypatch=monkeypatch))["kind"] == "delegation_not_declared"

    async def test_delegated_specialist_judged_by_own_delegates(self, monkeypatch):
        # THE fix: in a delegated turn origin["role"] is the PARENT
        # (assistant, which DECLARES finance) but execution_role is the
        # running delegate (mtg, which declares nothing). The ACL must key on
        # execution_role, so mtg->finance is DENIED even though the parent
        # would have been allowed. An impl keyed on `role` would wrongly PASS.
        _init({"assistant": _cfg("assistant", ["finance"]),
               "mtg": _cfg("mtg", []),
               "finance": _cfg("finance", [])})
        p = await _call("finance", role="assistant", execution_role="mtg",
                        monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"

    async def test_arg_role_spoof_ignored(self, monkeypatch):
        # Caller identity must come only from trusted origin. An attacker who
        # plants privileged role-like fields in the TOOL ARGS while running as
        # an unprivileged delegate (execution_role=mtg, declares nothing) is
        # still denied — the args are never consulted for identity.
        _init({"assistant": _cfg("assistant", ["finance"]),
               "mtg": _cfg("mtg", []),
               "finance": _cfg("finance", [])})
        p = await _call("finance", role="mtg", execution_role="mtg",
                        monkeypatch=monkeypatch,
                        extra_args={"role": "assistant",
                                    "execution_role": "assistant",
                                    "caller_role": "assistant"})
        assert p["kind"] == "delegation_not_declared"

    @pytest.mark.parametrize("mode", ["sync", "async", "interactive"])
    async def test_denial_uniform_across_modes(self, monkeypatch, mode):
        # The ACL denies before the mode/interactive branch, so an undeclared
        # target is refused identically regardless of requested mode.
        _init({"concierge": _cfg("concierge", ["mtg"]), "assistant": _cfg("assistant", ["finance"])})
        p = await _call("assistant", role="concierge", mode=mode, monkeypatch=monkeypatch)
        assert p["kind"] == "delegation_not_declared"
