"""Specialist memory read+write on the shared casa bank (tiered, plan 3).

_run_delegated_agent now inherits the PARENT context's channel clearance for
both read (delegated_recall → casa bank) and write (retain_delegated →
tier-classified, voice-gated).  The legacy MemoryProvider / add_turn / meta
session helpers are gone.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import tools

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fake semantic memory — mirrors _Sem from test_delegated_memory.py
# ---------------------------------------------------------------------------


class _FakeSem:
    def __init__(self, recall_ret: str = ""):
        self.recall_calls: list[dict] = []
        self.retain_calls: list[dict] = []
        self._recall_ret = recall_ret

    async def recall(self, bank, query, *, tags, max_tokens, budget="mid", **kw):
        self.recall_calls.append({
            "bank": bank,
            "query": query,
            "tags": sorted(tags),
            "max_tokens": max_tokens,
            "budget": budget,
        })
        return self._recall_ret

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


# ---------------------------------------------------------------------------
# Fake SDK client — mirrors _FakeSpecialistClient pattern
# ---------------------------------------------------------------------------


class _FakeSDKClient:
    """Minimal ClaudeSDKClient stand-in that captures the prompt and yields a reply."""

    captured_prompt: str = ""
    response_text: str = "specialist reply"

    @classmethod
    def reset(cls, response: str = "specialist reply") -> None:
        cls.captured_prompt = ""
        cls.response_text = response

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, text: str) -> None:
        type(self).captured_prompt = text

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        try:
            block = TextBlock(text=type(self).response_text)
        except TypeError:
            block = TextBlock(type(self).response_text)  # type: ignore[call-arg]
        try:
            asst = AssistantMessage(content=[block])
        except TypeError:
            asst = AssistantMessage.__new__(AssistantMessage)
            asst.content = [block]  # type: ignore[attr-defined]
        yield asst


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _specialist_cfg(role: str = "finance", token_budget: int = 4000):
    from config import (
        AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig,
    )
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, 
        role=role,
        model="claude-sonnet-4-6",
        system_prompt=f"You are {role}",
        character=CharacterConfig(name=role.capitalize()),
        enabled=True,
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=token_budget),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


def _set_origin(monkeypatch, *, channel: str = "telegram", role: str = "assistant",
                chat_id: str = "abc", cid: str = "cid42", scope: str = "personal") -> None:
    import agent as agent_mod
    agent_mod.origin_var.set({
        "role": role,
        "channel": channel,
        "chat_id": chat_id,
        "cid": cid,
        "scope": scope,
        "delegation_depth": 0,
    })


async def _drain_bg() -> None:
    bg = getattr(tools, "_specialist_bg_tasks", set())
    if bg:
        await asyncio.gather(*list(bg), return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 1: Read at inherited clearance (telegram → private + broader)
# ---------------------------------------------------------------------------


async def test_read_at_telegram_clearance(monkeypatch):
    """parent channel=telegram → recall tags include private,family,friends,public."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    task_text = "how is Q1 cashflow?"
    digest = "## Summary\nQ1 spend: €1200\n"
    fake_sem = _FakeSem(recall_ret=digest)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(cfg, task_text=task_text, context_text="")

    assert len(fake_sem.recall_calls) == 1
    c = fake_sem.recall_calls[0]
    assert c["bank"] == "casa"
    assert c["query"] == task_text
    assert c["tags"] == ["family", "friends", "private", "public"]

    prompt = _FakeSDKClient.captured_prompt
    assert '<memory_context agent="finance">' in prompt
    assert "Q1 spend" in prompt
    assert "</memory_context>" in prompt
    # Ordering: delegation_context → memory_context → Task:
    assert prompt.index("<delegation_context>") < prompt.index('<memory_context') < prompt.index("Task:")


# ---------------------------------------------------------------------------
# Test 2: token_budget=0 → no recall, no retain
# ---------------------------------------------------------------------------


async def test_token_budget_zero_no_sem_calls(monkeypatch):
    """token_budget=0 preserves the stateless path — no recall and no retain."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=0)
    fake_sem = _FakeSem(recall_ret="something")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    await _drain_bg()
    assert output.text == "ok"
    assert fake_sem.recall_calls == []
    assert fake_sem.retain_calls == []


# ---------------------------------------------------------------------------
# Test 3: Write via retain (telegram, non-empty reply)
# ---------------------------------------------------------------------------


async def test_write_retain_telegram(monkeypatch):
    """Telegram parent → retain fires; items have correct turns + document_ids."""
    import agent as agent_mod
    import delegated_memory

    async def fake_classify(text):
        return "private" if "cashflow" in text.lower() else "friends"

    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram", cid="cid42")
    _FakeSDKClient.reset(response="Q1 is on track")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(cfg, task_text="Q1 cashflow?", context_text="")

    await _drain_bg()

    assert len(fake_sem.retain_calls) == 1
    rc = fake_sem.retain_calls[0]
    assert rc["bank"] == "casa"
    items = rc["items"]
    assert len(items) == 2  # user turn + assistant turn
    assert items[0]["document_id"] == "delegation:cid42:finance:0"
    assert items[1]["document_id"] == "delegation:cid42:finance:1"
    # classify stub: "cashflow" in user text → private; assistant text no → friends
    assert items[0]["tags"] == ["private"]
    assert items[1]["tags"] == ["friends"]


# ---------------------------------------------------------------------------
# Test 4: Voice writes nothing (recall still fires)
# ---------------------------------------------------------------------------


async def test_voice_writes_nothing(monkeypatch):
    """Voice parent → write-trust gate fires; zero retain calls.
    Recall still fires (voice read-clearance = friends+public)."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="some prior fact")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="voice")
    _FakeSDKClient.reset(response="voice answer")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(cfg, task_text="hello?", context_text="")

    await _drain_bg()

    assert fake_sem.retain_calls == []
    # Recall did fire, with voice clearance tags
    assert len(fake_sem.recall_calls) == 1
    assert fake_sem.recall_calls[0]["tags"] == ["friends", "public"]


# ---------------------------------------------------------------------------
# Test 5: Empty reply → no retain
# ---------------------------------------------------------------------------


async def test_empty_reply_no_retain(monkeypatch):
    """If the SDK produces no text, retain must not fire."""
    import agent as agent_mod
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    fake_sem = _FakeSem(recall_ret="")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake_sem, raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="")  # empty SDK reply

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    await _drain_bg()
    assert fake_sem.retain_calls == []


# ---------------------------------------------------------------------------
# Test 5b: Recall unavailable → explicit status note, never a fake digest
# ---------------------------------------------------------------------------


async def test_recall_unavailable_injects_status_note(monkeypatch):
    """When memory could not be checked, the specialist is told so explicitly
    — a silent cold turn would let it claim Casa lacks information."""
    import agent as agent_mod
    from semantic_memory import RecallUnavailable

    class _DownSem(_FakeSem):
        async def recall(self, *a, **k):
            raise RecallUnavailable("http_504")

    cfg = _specialist_cfg(role="finance", token_budget=4000)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", _DownSem(), raising=False)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    assert output.text == "ok"                      # turn still completes
    prompt = _FakeSDKClient.captured_prompt
    assert '<memory_context agent="finance" status="unavailable">' in prompt
    assert "could not be checked" in prompt
    # No fabricated digest content beyond the status note.


# ---------------------------------------------------------------------------
# Test 6: Legacy helpers are gone
# ---------------------------------------------------------------------------


def test_legacy_helpers_removed():
    """The bespoke add_turn / meta-write helpers must have been deleted."""
    assert not hasattr(tools, "_specialist_add_turn_bg"), (
        "_specialist_add_turn_bg still exists — should have been removed"
    )
    assert not hasattr(tools, "_specialist_meta_write_bg"), (
        "_specialist_meta_write_bg still exists — should have been removed"
    )


# ---------------------------------------------------------------------------
# Test 7: Boot-degraded (active_semantic_memory is None) — no crash
# ---------------------------------------------------------------------------


async def test_sem_none_no_crash(monkeypatch):
    """active_semantic_memory unset (boot-degraded) → specialist runs, gets the
    unavailability note (memory can't be checked ≠ no memories), no crash."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _set_origin(monkeypatch, channel="telegram")
    _FakeSDKClient.reset(response="ok")

    with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
        output = await tools._run_delegated_agent(cfg, task_text="hi", context_text="")

    assert output.text == "ok"
    prompt = _FakeSDKClient.captured_prompt
    assert '<memory_context agent="finance" status="unavailable">' in prompt
    assert "could not be checked" in prompt
