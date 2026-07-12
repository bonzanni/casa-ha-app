"""Tests for engage_executor tool (Plan 3 real implementation)."""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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
        # types) must surface as MCP is_error so sdk_logging emits ok=False
        # — the tool didn't actually spawn an engagement, even though
        # Ellen's user-facing narration is graceful. Key is snake_case
        # because claude_agent_sdk reads ``result.get("is_error", False)``
        # at the MCP-server boundary.
        assert r.get("is_error") is True, (
            f"engage_executor must set is_error=True for unknown/disabled "
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
        assert r.get("is_error") is True

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

    async def test_non_telegram_origin_gets_accurate_error(self):
        """R-2 (v0.69.7): when the engagement machinery is unavailable for a
        non-Telegram origin, the error must accurately say engagements
        originate from Telegram — NOT the misleading 'set
        telegram_engagement_supergroup_id' message (which telegram-origin
        callers still get, see test_engagement_not_configured)."""
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg, channel_ok=False)  # supergroup unavailable on this origin

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "voice",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "engagement_wrong_origin"
        assert "Telegram" in payload["message"]
        assert "supergroup" not in payload["message"].lower()

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


class TestDuplicateTaskGuard:
    """P32 (v0.37.10): tool-level guard against engage_executor
    cumulative-context bleed. When a back-to-back assistant turn fires
    a duplicate engage_executor call (re-emitting the prior turn's
    task), the second call must be refused with kind=duplicate_task.

    Live evidence: docs/bug-review-2026-05-14-exploration6.md::O-6 —
    Ellen's O-6.2 turn fired two engage_executor calls in a single
    assistant message; the first carried the O-6.1 rename task.

    The guard uses Jaccard word similarity against the most-recent
    engagement for the same (channel, chat_id) within a 60s window.
    Computed in tools.py over the real engagement_registry, so this
    test uses a real registry (not MagicMock).
    """

    async def _real_registry(self, tmp_path):
        from engagement_registry import EngagementRegistry
        return EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None,
        )

    async def _setup_with_real_registry(
        self, tmp_path, monkeypatch, *, prompt="t",
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        registry = await self._real_registry(tmp_path)

        defn = _mock_executor_def()
        # Real prompt file so build path doesn't crash.
        p = tmp_path / "prompt.md"
        p.write_text(prompt)
        defn.prompt_template_path = str(p)

        exec_reg = MagicMock()
        exec_reg.get = MagicMock(return_value=defn)
        exec_reg.list_types = MagicMock(return_value=["configurator"])

        channel = MagicMock()
        channel.engagement_supergroup_id = -100123
        channel.engagement_permission_ok = True
        channel.open_engagement_topic = AsyncMock(return_value=42)
        channel.bot = MagicMock()
        channel.bot.edit_forum_topic = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)

        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=registry,
            executor_registry=exec_reg,
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver",
            MagicMock(start=AsyncMock()), raising=False,
        )
        return engage_executor, registry, channel

    async def test_distinct_tasks_both_succeed(self, tmp_path, monkeypatch):
        """Sanity: two engage_executor calls with non-overlapping tasks
        in the same channel/session must both succeed."""
        import agent as agent_mod
        engage_executor, registry, _ = await self._setup_with_real_registry(
            tmp_path, monkeypatch,
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r1 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": "rename the agent name from its current value to Ellen-A and back",
                "context": "",
            })
            r2 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": "build a brand new repo for the casa probe artifact bundle",
                "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        p1 = json.loads(r1["content"][0]["text"])
        p2 = json.loads(r2["content"][0]["text"])
        assert p1["status"] == "pending"
        assert p2["status"] == "pending"
        # Both engagements landed in the registry.
        assert len(registry._records) == 2

    async def test_duplicate_task_blocked(self, tmp_path, monkeypatch):
        """The load-bearing case: a back-to-back duplicate task is
        refused with kind=duplicate_task. Two configurator spawns with
        near-identical task text must result in exactly one engagement
        in the registry; the second returns is_error=True."""
        import agent as agent_mod
        engage_executor, registry, _ = await self._setup_with_real_registry(
            tmp_path, monkeypatch,
        )
        task1 = (
            "rename the agent name from its current value to Ellen-A and "
            "then back to the default"
        )
        # Near-duplicate (identical lead, slight phrasing variation) —
        # the bleed pattern observed live in exploration6.
        task2 = (
            "rename the agent name from its current value to Ellen-A and "
            "then back to the default value"
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r1 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task1, "context": "",
            })
            r2 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task2, "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        p1 = json.loads(r1["content"][0]["text"])
        p2 = json.loads(r2["content"][0]["text"])
        assert p1["status"] == "pending"
        assert p2["status"] == "error", (
            f"second engage_executor with duplicate task must be refused, "
            f"got {p2!r}"
        )
        assert p2["kind"] == "duplicate_task"
        # MCP envelope must carry is_error=True so sdk_logging emits ok=False.
        assert r2.get("is_error") is True
        # Exactly one engagement landed.
        assert len(registry._records) == 1

    async def test_other_channel_does_not_block(self, tmp_path, monkeypatch):
        """A duplicate task in a DIFFERENT channel must not block the
        new spawn. Cross-channel isolation."""
        import agent as agent_mod
        engage_executor, registry, _ = await self._setup_with_real_registry(
            tmp_path, monkeypatch,
        )
        task = (
            "rename the agent name from its current value to Ellen-A and "
            "then back to the default"
        )
        token1 = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r1 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task, "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token1)

        # Different channel — guard must not block.
        token2 = agent_mod.origin_var.set({
            "role": "assistant", "channel": "discord",
            "chat_id": "c1", "cid": "y", "user_text": "hi",
        })
        try:
            r2 = await engage_executor.handler({
                "executor_type": "configurator",
                "task": task, "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token2)

        p1 = json.loads(r1["content"][0]["text"])
        p2 = json.loads(r2["content"][0]["text"])
        assert p1["status"] == "pending"
        assert p2["status"] == "pending"
        assert len(registry._records) == 2


class TestEngageExecutorClaudeCode:
    @pytest.mark.skip(reason="TODO(Phase G): Full wiring test — covered by D-block E2E")
    async def test_dispatches_to_claude_code_driver(self, monkeypatch, tmp_path):
        """When executor.driver == 'claude_code', engage_executor calls the
        claude_code driver with the ExecutorDefinition as options."""
        # See TestEngageExecutorConfigurator for the setup pattern. The real
        # coverage lands in the E2E D-block against the mock CLI.
        pass


class TestU3TopicTitle:
    """E-12 (v0.37.0) Task 22: U3 state-encoded topic title at engagement open.

    Spec §6.3 (revised v0.37.1 D-1): ``<state-emoji> <concise task>`` — no
    engagement-id suffix. The role icon now lives on the bubble (numeric
    custom_emoji_id via channels.topic_icons), not in the title text.
    """

    async def test_engage_executor_opens_topic_with_state_encoded_title(
        self, tmp_path, monkeypatch,
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def(type="plugin-developer")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["plugin-developer"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        er.set_channel_state = AsyncMock()

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
            await engage_executor.handler({
                "executor_type": "plugin-developer",
                "task": "Please add a Skill for the casa-probe-foo plugin",
                "context": "none",
            })
        finally:
            agent_mod.origin_var.reset(token)

        # 1. open_engagement_topic called with the U3-shaped title.
        channel.open_engagement_topic.assert_awaited_once()
        kwargs = channel.open_engagement_topic.await_args.kwargs
        name = kwargs["name"]
        # v0.37.1 D-1: title is "<state> <task>" — role icon is on the
        # bubble (kwargs["role"]), not in the title.
        assert name.startswith("🟢 "), f"got {name!r}"
        assert "Skill" in name
        # No engagement-id suffix.
        assert " | " not in name
        # Role is passed as a kwarg (resolves to numeric custom_emoji_id
        # inside open_engagement_topic).
        assert kwargs["role"] == "plugin-developer"
        # 2. set_channel_state(current_state_emoji="🟢") persisted.
        er.set_channel_state.assert_awaited()
        emoji_calls = [
            kw.get("current_state_emoji")
            for _, kw in er.set_channel_state.await_args_list
        ]
        assert "🟢" in emoji_calls

    async def test_engage_executor_unknown_role_falls_back_to_robot_emoji(
        self, tmp_path, monkeypatch,
    ):
        """v0.37.1 D-1: unknown executor_type → bubble falls back to
        DEFAULT_ROLE_ID (🤖). Title format no longer encodes the role."""
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def(type="exotic-future-type")
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["exotic-future-type"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "x" * 32
        mock_rec.topic_id = 7
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        er.set_channel_state = AsyncMock()

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
            await engage_executor.handler({
                "executor_type": "exotic-future-type",
                "task": "do the new thing",
                "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)

        kwargs = channel.open_engagement_topic.await_args.kwargs
        # Title is just "<state> <task>" — role no longer in title.
        assert kwargs["name"].startswith("🟢 ")
        # Role passes through verbatim; open_engagement_topic resolves
        # it to DEFAULT_ROLE_ID via icon_id_for_role.
        assert kwargs["role"] == "exotic-future-type"


class TestOriginContextPropagation:
    """L61/L10: engage_executor's context= argument (and the world-state
    summary) must be threaded onto the EngagementRecord's origin so the
    claude_code driver can later render them into the workspace CLAUDE.md.
    Before the fix, origin=dict(origin_var) never carried a 'context' key,
    so the driver's engagement.origin.get('context', '') was always empty."""

    async def test_context_and_world_state_land_in_created_origin(
        self, tmp_path, monkeypatch,
    ):
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
        er.recent_for_origin = MagicMock(return_value=None)

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
                "executor_type": "configurator", "task": "do it",
                "context": "repo is x/y, branch dev",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending"

        created_origin = er.create.await_args.kwargs["origin"]
        assert created_origin["context"] == "repo is x/y, branch dev"
        assert "world_state_summary" in created_origin
        # Original origin_var fields must still be present.
        assert created_origin["role"] == "assistant"
        assert created_origin["channel"] == "telegram"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_claude_code_driver_receives_context_and_world_state(
        self, monkeypatch, tmp_path,
    ):
        """Driver-side regression: ClaudeCodeDriver.start must read the
        origin's 'context'/'world_state_summary' back out and pass them
        into provision_workspace. Follows the mocking pattern of
        tests/test_claude_code_driver.py::TestStart."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc
        from engagement_registry import EngagementRecord

        async def fake_cau():
            pass

        async def fake_start_kw(*, engagement_id):
            pass

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)
        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc-root"),
        )
        (tmp_path / "svc-root").mkdir()
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: None,
        )

        async def _noop_write(self, engagement, text):
            return None
        monkeypatch.setattr(ClaudeCodeDriver, "_write_to_fifo", _noop_write)

        defn = _mock_executor_def(driver="claude_code")
        exec_dir = tmp_path / "defaults-executors" / "hello-driver"
        exec_dir.mkdir(parents=True)
        prompt_path = exec_dir / "prompt.md"
        prompt_path.write_text(
            "T:{task} C:{context} W:{world_state_summary}",
        )
        defn.prompt_template_path = str(prompt_path)

        rec = EngagementRecord(
            id="abc12345def67890", kind="executor", role_or_type="hello-driver",
            driver="claude_code", status="active", topic_id=999,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None,
            origin={
                "channel": "telegram", "chat_id": "42",
                "context": "repo is x/y, branch dev",
                "world_state_summary": "ws-summary",
            },
            task="do it",
        )

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()

        await drv.start(rec, prompt="system prompt body", options=defn)

        claude_md = (
            tmp_path / "engagements" / rec.id / "CLAUDE.md"
        ).read_text(encoding="utf-8")
        assert "C:repo is x/y, branch dev" in claude_md


class TestFailedStartClosesTopic:
    """L23 leak guard: a failed engagement start must not leave a
    permanently-open 'active' forum topic — it must be flipped to 'failed'
    and closed."""

    async def test_driver_start_failure_flips_and_closes_topic(
        self, tmp_path, monkeypatch,
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()  # driver="in_casa"
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        er.set_channel_state = AsyncMock()
        er.recent_for_origin = MagicMock(return_value=None)

        channel = await _setup(reg, tmp_path=tmp_path)
        channel.update_topic_state = AsyncMock()
        channel.close_topic = AsyncMock()
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
            MagicMock(start=AsyncMock(side_effect=RuntimeError("boom"))),
            raising=False,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await engage_executor.handler(
                {"executor_type": "configurator", "task": "do a thing"},
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "driver_start_failed"
        er.mark_error.assert_awaited_once()
        # The leak fix: the just-created topic must be flipped to failed and closed.
        channel.update_topic_state.assert_awaited_once_with(
            engagement_id=mock_rec.id, new_state="failed",
        )
        channel.close_topic.assert_awaited_once_with(thread_id=42)

    async def test_prompt_template_missing_flips_and_closes_topic(
        self, tmp_path, monkeypatch,
    ):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        defn.prompt_template_path = "/nonexistent/prompt.md"
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()
        er.set_channel_state = AsyncMock()
        er.recent_for_origin = MagicMock(return_value=None)

        channel = await _setup(reg, tmp_path=None)
        channel.update_topic_state = AsyncMock()
        channel.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await engage_executor.handler(
                {"executor_type": "configurator", "task": "do a thing"},
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "prompt_template_missing"
        channel.update_topic_state.assert_awaited_once_with(
            engagement_id=mock_rec.id, new_state="failed",
        )
        channel.close_topic.assert_awaited_once_with(thread_id=42)
