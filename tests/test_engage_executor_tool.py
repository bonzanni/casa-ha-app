"""Tests for engage_executor tool (Plan 3 real implementation)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _mock_executor_def(**overrides):
    from config import ExecutorDefinition
    defaults = {
        "type": "configurator",
        "description": "Test configurator type for engage_executor tests.",
        "model": "claude-sonnet-4-6",
        "driver": "in_casa",
        "enabled": True,
        "tools_allowed": ["Read"],
        "tools_disallowed": [],
        "permission_mode": "acceptEdits",
        "mcp_server_names": ["casa-framework"],
        "idle_reminder_days": 7,
        "prompt_template_path": "/tmp/nonexistent.md",
        "hooks_path": None,
        "observer_policy_path": None,
        "doctrine_dir": "/tmp/doctrine",
    }
    defaults.update(overrides)
    return ExecutorDefinition(**defaults)


async def _setup(
    executor_registry,
    channel_ok=True,
    prompt_template="You are {task}. Context: {context}. State: {world_state_summary}",
    tmp_path=None,
):
    from tools import init_tools
    if tmp_path is not None and executor_registry is not None:
        defn = executor_registry.get("configurator")
        if defn is not None:
            p = tmp_path / "prompt.md"
            p.write_text(prompt_template)
            defn.prompt_template_path = str(p)

    channel = MagicMock()
    channel.engagement_supergroup_id = -100123 if channel_ok else 0
    channel.engagement_permission_ok = channel_ok
    channel.open_engagement_topic = AsyncMock(return_value=42)
    channel.bot = MagicMock()
    channel.bot.edit_forum_topic = AsyncMock()
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)

    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        executor_registry=executor_registry,
    )
    return channel


class TestEngageExecutorReal:
    async def test_no_executor_types_when_registry_empty(self):
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=[])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "no_executor_types"

    async def test_unknown_type_error(self):
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=["other_type"])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "unknown_executor_type"
        # F-7 (v0.32.0): registry-rejected calls (e.g. disabled executor
        # types) must surface as MCP isError so sdk_logging emits ok=False
        # — the tool didn't actually spawn an engagement, even though
        # Ellen's user-facing narration is graceful.
        assert r.get("isError") is True, (
            f"engage_executor must set isError=True for unknown/disabled "
            f"executor types. envelope keys: {sorted(r.keys())}"
        )

    async def test_disabled_executor_type_returns_is_error(self):
        """F-7 (v0.32.0): the registry strips disabled executor entries
        from ``_defs``, so ``get(disabled_type)`` returns None and falls
        through the same ``unknown_executor_type`` path as truly-unknown
        names. The contract under test is the MCP envelope-level
        ``isError`` flag, exercised here through the disabled-executor
        live shape (P5 cid ``20a903c3`` from the 2026-05-02 exploration:
        plugin-developer was bundled but disabled, ``ok=True ms=7284``
        in the tool_result log even though no engagement spawned).
        """
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        # Real ExecutorRegistry behavior: disabled types are excluded
        # from _defs, so .get() returns None and .list_types() shows only
        # the enabled set (which may include other types).
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "plugin-developer",
                "task": "build a thing", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_executor_type"
        assert r.get("isError") is True

    async def test_engagement_not_configured(self):
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg, channel_ok=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "engagement_not_configured"

    async def test_ef_inline_retry_recovers_from_first_boot_race(
        self, tmp_path, monkeypatch,
    ):
        """E-F (v0.30.0) defensive: when supergroup IS configured but
        engagement_permission_ok is still False (first-boot race lost
        before _rebuild's tail setup ran), engage_executor must call
        setup_engagement_features() once in-line. If that retry flips
        the flag, the engagement proceeds normally — no manual restart
        required.
        """
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        # Channel with supergroup CONFIGURED but permission flag stuck
        # False. The _setup() helper supports `channel_ok=True` (both
        # set) and `channel_ok=False` (both cleared); we need the third
        # state, so build the channel by hand.
        channel = MagicMock()
        channel.engagement_supergroup_id = -100123
        channel.engagement_permission_ok = False
        channel.open_engagement_topic = AsyncMock(return_value=42)
        channel.bot = MagicMock()
        channel.bot.edit_forum_topic = AsyncMock()

        async def _flip_flag():
            channel.engagement_permission_ok = True

        channel.setup_engagement_features = AsyncMock(side_effect=_flip_flag)

        # Wire the prompt template so build_prompt does not blow up.
        p = tmp_path / "prompt.md"
        p.write_text("You are {task}. Context: {context}.")
        defn.prompt_template_path = str(p)

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()

        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        # In-line retry was attempted — exactly once.
        assert channel.setup_engagement_features.await_count == 1
        # Retry succeeded (flag flipped) → engagement opened normally.
        assert payload["status"] == "pending", payload
        assert payload["topic_id"] == 42

    async def test_ef_inline_retry_does_not_fire_when_supergroup_unset(self):
        """E-F retry is gated on supergroup-configured-but-flag-False.
        When supergroup is unset, no retry should fire — the operator
        hasn't opted into engagements at all.
        """
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        # Build a channel with supergroup explicitly UNSET. Don't use
        # _setup(channel_ok=False) because that doesn't expose the
        # AsyncMock we want to assert on.
        channel = MagicMock()
        channel.engagement_supergroup_id = 0   # unset
        channel.engagement_permission_ok = False
        channel.setup_engagement_features = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        from tools import init_tools
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            executor_registry=reg,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "engagement_not_configured"
        # No retry attempted — supergroup is unset, no point.
        assert channel.setup_engagement_features.await_count == 0

    async def test_happy_path_returns_pending(self, tmp_path, monkeypatch):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )

        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                            MagicMock(start=AsyncMock()), raising=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "make a thing",
                "context": "none",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["executor_type"] == "configurator"
        assert payload["topic_id"] == 42

    async def test_requires_origin(self):
        from tools import engage_executor, init_tools
        reg = MagicMock()
        reg.list_types = MagicMock(return_value=[])
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            executor_registry=reg,
        )
        r = await engage_executor.handler({"executor_type": "configurator", "task": "t"})
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "no_origin"

    async def test_does_not_leak_engagement_var_to_caller(
        self, tmp_path, monkeypatch,
    ):
        """engage_executor must not bind engagement_var in the engager's scope.

        The tool dispatches to driver.start, which (post-Phase-1) sets
        engagement_var only inside _deliver_turn. The engager's task must
        observe engagement_var == None both before and after the call.
        """
        from tools import engage_executor, engagement_var, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            assert engagement_var.get(None) is None  # pre-state
            r = await engage_executor.handler({
                "executor_type": "configurator",
                "task": "t",
                "context": "",
            })
            assert engagement_var.get(None) is None  # post-state
        finally:
            agent_mod.origin_var.reset(token)

        # Sanity: handler still completed with the expected envelope shape
        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending"


class TestEngageExecutorClaudeCode:
    @pytest.mark.skip(reason="TODO(Phase G): Full wiring test — covered by D-block E2E")
    async def test_dispatches_to_claude_code_driver(self, monkeypatch, tmp_path):
        """When executor.driver == 'claude_code', engage_executor calls the
        claude_code driver with the ExecutorDefinition as options."""
        # See TestEngageExecutorConfigurator for the setup pattern. The real
        # coverage lands in the E2E D-block against the mock CLI.
        pass
