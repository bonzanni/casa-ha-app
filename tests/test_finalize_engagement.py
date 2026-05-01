"""Tests for the shared _finalize_engagement helper in tools.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestFinalizeEngagement:
    async def test_happy_path_closes_topic_and_notifies_ellen(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "12345"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.close_topic_with_check = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()
        memory = MagicMock()
        memory.add_turn = AsyncMock()
        memory.ensure_session = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = MagicMock()
        driver.cancel = AsyncMock()

        await _finalize_engagement(
            rec, outcome="completed", text="summary", artifacts=["sha1"],
            next_steps=[], driver=driver, memory_provider=memory,
        )

        # Topic closed + icon flipped
        telegram.close_topic_with_check.assert_awaited_once_with(thread_id=42)
        # Completion message posted in topic
        telegram.send_to_topic.assert_awaited()
        # NOTIFICATION sent to Ellen
        bus.notify.assert_awaited_once()
        # Driver cancelled
        driver.cancel.assert_awaited_once_with(rec)
        # Meta-scope summary written
        memory.add_turn.assert_awaited_once()
        # Record status is completed
        assert rec.status == "completed"
        assert rec.completed_at is not None

    async def test_cancel_outcome_uses_cancel_path(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.close_topic_with_check = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = MagicMock()
        driver.cancel = AsyncMock()

        await _finalize_engagement(
            rec, outcome="cancelled", text="user cancelled",
            artifacts=[], next_steps=[], driver=driver, memory_provider=None,
        )
        assert rec.status == "cancelled"
        driver.cancel.assert_awaited_once_with(rec)


async def test_meta_summary_write_retries_once_on_tls_eof(tmp_path, caplog):
    """F-4 (v0.32.0): the Honcho client's reused TLS connection can
    surface ``Connection error: TLS/SSL connection has been closed (EOF)``
    on the next request after a long idle. Live evidence:
    P4.2 cid ``0fb4428d`` from the 2026-05-02 exploration —
    engagement still finalized ``outcome=completed``, but the M4 meta-
    scope summary was lost.

    Contract: when the first attempt at ``ensure_session`` /
    ``add_turn`` raises a transient connection error, the helper retries
    once before giving up. Successful retry → no WARNING, summary lands.
    """
    import logging
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "12345"},
        topic_id=42,
    )

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic_with_check = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    # ensure_session fires twice if add_turn succeeds on second pass
    # (the retry loop runs the whole block again). Track per-call.
    ensure_calls = {"n": 0}

    async def _ensure_first_call_raises(**kwargs):
        ensure_calls["n"] += 1
        if ensure_calls["n"] == 1:
            raise Exception(
                "Connection error: TLS/SSL connection has been closed (EOF) (_ssl.c:992)",
            )

    memory = MagicMock()
    memory.ensure_session = AsyncMock(side_effect=_ensure_first_call_raises)
    memory.add_turn = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    with caplog.at_level(logging.INFO, logger="tools"):
        await _finalize_engagement(
            rec, outcome="completed", text="ok",
            artifacts=[], next_steps=[], driver=driver, memory_provider=memory,
        )

    # Two ensure_session attempts (first raised, second succeeded).
    assert ensure_calls["n"] == 2, (
        f"expected exactly 2 ensure_session attempts (one retry); "
        f"got {ensure_calls['n']}"
    )
    # add_turn fired once on the successful retry.
    memory.add_turn.assert_awaited_once()
    # No WARNING-level "meta summary write failed" line — the retry
    # succeeded.
    failed = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "meta summary write failed" in r.getMessage()
    ]
    assert not failed, (
        f"unexpected WARNING after successful retry: "
        f"{[r.getMessage() for r in failed]}"
    )
    # An INFO-level "retrying once" breadcrumb confirms we hit the retry path.
    retried = [
        r for r in caplog.records
        if "retrying once" in r.getMessage()
    ]
    assert retried, (
        "expected an INFO breadcrumb noting the retry on transient "
        "connection error"
    )


async def test_meta_summary_write_does_not_retry_on_non_transient_error(tmp_path, caplog):
    """F-4 companion: non-connection errors (programming bugs, schema
    rejects, auth failures) must NOT trigger the retry — they will not
    self-heal on a second attempt and only delay finalize."""
    import logging
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "12345"},
        topic_id=42,
    )

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic_with_check = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    ensure_calls = {"n": 0}

    async def _always_raises_value_error(**kwargs):
        ensure_calls["n"] += 1
        raise ValueError("schema rejected: bad agent_role 'foo:bar'")

    memory = MagicMock()
    memory.ensure_session = AsyncMock(side_effect=_always_raises_value_error)
    memory.add_turn = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="tools"):
        await _finalize_engagement(
            rec, outcome="completed", text="ok",
            artifacts=[], next_steps=[], driver=driver, memory_provider=memory,
        )

    # Single attempt — no retry, no add_turn.
    assert ensure_calls["n"] == 1, (
        f"non-transient errors must not retry; got {ensure_calls['n']} attempts"
    )
    memory.add_turn.assert_not_awaited()
    # WARNING line still fires (best-effort write failed).
    failed = [
        r for r in caplog.records
        if "meta summary write failed" in r.getMessage()
    ]
    assert failed, "expected WARNING when non-transient error swallows the write"


async def test_finalize_writes_retention_for_claude_code_driver(
    tmp_path, monkeypatch,
):
    """Plan 4a.1: _finalize_engagement must update .casa-meta.json with
    retention_until = now + 7 days when driver=='claude_code' and a
    workspace dir exists."""
    import json
    import tools as tools_mod
    from engagement_registry import EngagementRecord, EngagementRegistry
    from drivers.workspace import write_casa_meta
    from tools import _finalize_engagement

    ws = tmp_path / "eng1"
    ws.mkdir()
    write_casa_meta(
        workspace_path=str(ws), engagement_id="eng1",
        executor_type="hello-driver", status="UNDERGOING",
        created_at="2026-04-23T08:00:00Z",
        finished_at=None, retention_until=None,
    )

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = EngagementRecord(
        id="eng1", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    reg._records["eng1"] = rec

    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_channel_manager", None)
    monkeypatch.setattr(tools_mod, "_bus", None)
    # Point the hardcoded /data/engagements path to tmp.
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    await _finalize_engagement(
        rec, outcome="completed", text="done",
        artifacts=[], next_steps=[], driver=None, memory_provider=None,
    )

    meta = json.loads((ws / ".casa-meta.json").read_text())
    assert meta["status"] == "COMPLETED"
    assert meta["retention_until"] is not None
    # Parseable as ISO 8601 Z.
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        meta["retention_until"])


async def test_executor_finalize_archives_under_hyphen_role_id(
    tmp_path, monkeypatch,
):
    """Bug 6: _finalize_engagement's per-type executor archive branch
    (tools.py:1525-1555 in master tip f3f72a1) writes under
    agent_role='executor-<type>' (hyphen, not colon — Honcho regex
    ^[A-Za-z0-9_-]+$ rejects colon)."""
    import tools as tools_mod
    from engagement_registry import EngagementRecord, EngagementRegistry
    from tools import _finalize_engagement

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "tomb.json"), bus=None,
    )
    rec = EngagementRecord(
        id="eng2", kind="executor", role_or_type="configurator",
        driver="in_casa", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None,
        origin={"channel": "telegram", "chat_id": "42",
                "role": "assistant"},
        task="t",
    )
    reg._records["eng2"] = rec

    # Patch tools.py module globals so _finalize_engagement runs without
    # a live channel manager / bus / engagement registry.
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_channel_manager", None)
    monkeypatch.setattr(tools_mod, "_bus", None)

    memory = MagicMock()
    memory.ensure_session = AsyncMock()
    memory.add_turn = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="done",
        artifacts=[], next_steps=[], driver=None,
        memory_provider=memory,
    )

    # _finalize_engagement makes two memory writes for kind="executor":
    # the meta-scope summary first, then the executor-archive. The
    # executor-archive write must use 'executor-<type>' (hyphen) — Bug 6
    # is specifically about the second one. No call may use a colon.
    es_roles = [
        c.kwargs.get("agent_role")
        for c in memory.ensure_session.await_args_list
    ]
    at_roles = [
        c.kwargs.get("agent_role")
        for c in memory.add_turn.await_args_list
    ]
    assert "executor-configurator" in es_roles, (
        f"executor-archive ensure_session not found; got: {es_roles}"
    )
    assert "executor-configurator" in at_roles, (
        f"executor-archive add_turn not found; got: {at_roles}"
    )
    for r in es_roles + at_roles:
        if r is not None:
            assert ":" not in r, f"colon-keyed Honcho id leaked: {r!r}"
