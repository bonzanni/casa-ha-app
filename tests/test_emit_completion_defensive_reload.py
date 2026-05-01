"""G-2 hotfix (v0.33.1): emit_completion must force-call casa_reload
when an engagement committed a real change via config_git_commit but
never invoked any reload tool.

Background — exploration2 (2026-05-01) finding G-2 reproduced on
v0.33.0's doctrine-only fix (live verify cid `a9313680` 11:39:57Z):
configurator read the inverted-order completion.md + reload.md, then
still skipped the reload tool_use. The model's narration even claimed
"calling casa_reload now" without making the call. Same false-positive
pattern as pre-v0.33.0.

Defensive guard (per kickoff option b): track per-engagement whether a
reload was satisfied via a module-level set. config_git_commit
populates the set on real-SHA commits; casa_reload + casa_reload_triggers
drain it; emit_completion force-calls casa_reload when still pending,
emits a WARNING, and proceeds to _finalize_engagement.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _drain_pending_set():
    """Tests share module-level state — drain the set per-test so the
    engagement-ids from earlier cases don't leak into later ones."""
    import tools as tools_mod
    yield
    tools_mod._ENGAGEMENTS_PENDING_RELOAD.clear()


async def test_committed_yaml_no_reload_force_calls_casa_reload(
    tmp_path, caplog, monkeypatch, _drain_pending_set,
):
    """When config_git_commit landed a real SHA on this engagement and
    emit_completion fires before any reload tool was called, the guard
    must force-call casa_reload and emit a WARNING."""
    from engagement_registry import EngagementRegistry
    from tools import (
        emit_completion, engagement_var,
        _ENGAGEMENTS_PENDING_RELOAD,
        init_tools,
    )

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "1"},
        topic_id=42,
    )
    init_tools(
        channel_manager=None, bus=None,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    # Simulate config_git_commit having registered this engagement —
    # in the real flow it does this after the SHA is non-empty.
    _ENGAGEMENTS_PENDING_RELOAD.add(rec.id)

    # Stub casa_reload (the tool body) so we don't actually POST to a
    # nonexistent Supervisor in the test environment.
    forced_calls = []

    async def _fake_casa_reload(_args: dict) -> dict:
        forced_calls.append(_args)
        return {"content": [{"type": "text", "text": '{"supervisor_status": 200}'}]}

    monkeypatch.setattr("tools.casa_reload.handler", _fake_casa_reload)

    token = engagement_var.set(rec)
    try:
        with caplog.at_level(logging.WARNING, logger="tools"):
            await emit_completion.handler({
                "status": "ok", "text": "done", "artifacts": [], "next_steps": [],
            })
    finally:
        engagement_var.reset(token)

    # casa_reload must have been force-called.
    assert len(forced_calls) == 1, (
        f"expected exactly one forced casa_reload, got {len(forced_calls)}"
    )

    # WARNING must mention the engagement id and the guard reason.
    guard_lines = [
        r for r in caplog.records
        if "outstanding reload obligation" in r.getMessage()
    ]
    assert guard_lines, (
        "expected a WARNING citing the outstanding-reload obligation; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert rec.id[:8] in guard_lines[0].getMessage()
    assert guard_lines[0].levelno == logging.WARNING

    # Set must be drained so a subsequent emit_completion (idempotent
    # re-call) doesn't re-trigger.
    assert rec.id not in _ENGAGEMENTS_PENDING_RELOAD


async def test_reload_already_called_skips_force_call(
    tmp_path, monkeypatch, _drain_pending_set,
):
    """Happy path: casa_reload was invoked during the engagement
    (drained the pending set). emit_completion must NOT force-call."""
    from engagement_registry import EngagementRegistry
    from tools import (
        emit_completion, engagement_var,
        _ENGAGEMENTS_PENDING_RELOAD,
        init_tools,
    )

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=42,
    )
    init_tools(
        channel_manager=None, bus=None,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    # Simulate the canonical path: commit registered, then reload
    # drained.
    _ENGAGEMENTS_PENDING_RELOAD.add(rec.id)
    _ENGAGEMENTS_PENDING_RELOAD.discard(rec.id)

    forced_calls = []

    async def _fake_casa_reload(_args: dict) -> dict:
        forced_calls.append(_args)
        return {"content": [{"type": "text", "text": '{"supervisor_status": 200}'}]}

    monkeypatch.setattr("tools.casa_reload.handler", _fake_casa_reload)

    token = engagement_var.set(rec)
    try:
        await emit_completion.handler({
            "status": "ok", "text": "done", "artifacts": [], "next_steps": [],
        })
    finally:
        engagement_var.reset(token)

    assert forced_calls == [], (
        "casa_reload must NOT be force-called when the engagement "
        f"already drained the pending set; got: {forced_calls}"
    )


async def test_no_commit_no_force_call(
    tmp_path, monkeypatch, _drain_pending_set,
):
    """If no config_git_commit was issued, the engagement is not in
    the pending set and emit_completion must not force-call."""
    from engagement_registry import EngagementRegistry
    from tools import (
        emit_completion, engagement_var,
        _ENGAGEMENTS_PENDING_RELOAD,
        init_tools,
    )

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=42,
    )
    init_tools(
        channel_manager=None, bus=None,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    # Note: NOT adding rec.id to _ENGAGEMENTS_PENDING_RELOAD.

    forced_calls = []

    async def _fake_casa_reload(_args: dict) -> dict:
        forced_calls.append(_args)
        return {"content": [{"type": "text", "text": '{"supervisor_status": 200}'}]}

    monkeypatch.setattr("tools.casa_reload.handler", _fake_casa_reload)

    token = engagement_var.set(rec)
    try:
        await emit_completion.handler({
            "status": "ok", "text": "no-op engagement", "artifacts": [], "next_steps": [],
        })
    finally:
        engagement_var.reset(token)

    assert forced_calls == [], (
        "casa_reload must NOT be force-called when no commit was "
        f"made; got: {forced_calls}"
    )


async def test_outcome_error_does_not_force_call(
    tmp_path, monkeypatch, _drain_pending_set,
):
    """outcome=error means the engagement bailed; even if a commit
    landed earlier, force-calling reload on a failed engagement is
    overreach. The set still gets drained at the end of
    emit_completion to avoid stale state."""
    from engagement_registry import EngagementRegistry
    from tools import (
        emit_completion, engagement_var,
        _ENGAGEMENTS_PENDING_RELOAD,
        init_tools,
    )

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=42,
    )
    init_tools(
        channel_manager=None, bus=None,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    _ENGAGEMENTS_PENDING_RELOAD.add(rec.id)

    forced_calls = []

    async def _fake_casa_reload(_args: dict) -> dict:
        forced_calls.append(_args)
        return {"content": [{"type": "text", "text": '{"supervisor_status": 200}'}]}

    monkeypatch.setattr("tools.casa_reload.handler", _fake_casa_reload)

    token = engagement_var.set(rec)
    try:
        await emit_completion.handler({
            "status": "failed", "text": "schema validation failed",
            "artifacts": [], "next_steps": [],
        })
    finally:
        engagement_var.reset(token)

    assert forced_calls == [], (
        f"casa_reload must NOT be force-called on outcome=error; got: "
        f"{forced_calls}"
    )
    # State is still drained so a follow-up retry isn't haunted by
    # stale tracking.
    assert rec.id not in _ENGAGEMENTS_PENDING_RELOAD
