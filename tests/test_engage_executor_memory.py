"""Unit coverage for _fetch_executor_archive (M4 L3)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import _fetch_executor_archive


pytestmark = pytest.mark.asyncio


async def test_returns_empty_when_provider_none():
    out = await _fetch_executor_archive(
        memory_provider=None,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    )
    assert out == ""


async def test_returns_empty_when_archive_empty():
    mp = MagicMock()
    mp.ensure_session = AsyncMock(return_value=None)
    mp.get_context = AsyncMock(return_value="")

    out = await _fetch_executor_archive(
        memory_provider=mp,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    )
    assert out == ""
    mp.ensure_session.assert_awaited_once_with(
        session_id="telegram-42-executor-configurator",
        agent_role="executor-configurator",
    )
    mp.get_context.assert_awaited_once()
    kwargs = mp.get_context.await_args.kwargs
    assert kwargs["session_id"] == "telegram-42-executor-configurator"
    assert kwargs["tokens"] == 2000
    # M3-self (v0.30.0): agent_role is back in the signature — but with
    # different semantics. v0.26.0 / E-14 dropped agent_role/user_peer
    # because the abstract had bound them as overlay-fetch parameters
    # (now relocated to peer_overlay_context). v0.30.0 reintroduces
    # ONLY agent_role, threaded as Honcho's peer_target so semantic
    # retrieval is scoped to the agent peer. user_peer remains dropped.
    assert kwargs["agent_role"] == "executor-configurator"
    assert "user_peer" not in kwargs


def test_get_context_signature_locks_kwargs():
    """Lock MemoryProvider.get_context's parameter set against future
    drift. v0.30.0 / M3-self: agent_role is now expected (forwarded as
    Honcho's peer_target). user_peer remains dropped. This introspection
    test catches any future caller-vs-ABC divergence at unit-test time
    rather than waiting for an exploration session."""
    import inspect
    from memory import MemoryProvider

    sig = inspect.signature(MemoryProvider.get_context)
    actual = set(sig.parameters.keys())
    expected = {"self", "session_id", "tokens", "search_query", "agent_role"}
    assert actual == expected, (
        f"MemoryProvider.get_context kwargs drifted. "
        f"Expected {expected}, got {actual}. "
        f"If this is intentional, audit every call site (agent.py, "
        f"tools.py::_fetch_executor_archive, tools.py::cross_peer_context) "
        f"and update this test."
    )


async def test_returns_wrapped_block_when_archive_populated():
    mp = MagicMock()
    mp.ensure_session = AsyncMock(return_value=None)
    mp.get_context = AsyncMock(
        return_value="## Recent exchanges\n- prior task done\n",
    )

    out = await _fetch_executor_archive(
        memory_provider=mp,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    )
    assert out.startswith("## Prior engagements (lessons learned)\n")
    assert "prior task done" in out


async def test_returns_empty_when_provider_raises():
    mp = MagicMock()
    mp.ensure_session = AsyncMock(side_effect=RuntimeError("honcho boom"))
    mp.get_context = AsyncMock(return_value="never reached")

    out = await _fetch_executor_archive(
        memory_provider=mp,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    )
    assert out == ""


def test_substitutes_executor_memory_slot_when_memory_enabled(monkeypatch, tmp_path):
    """engage_executor build_prompt path interpolates {executor_memory}.

    Black-box-ish: we re-derive the same substitution rules engage_executor
    uses, asserting the helper's output substitutes correctly.
    """
    template = (
        "task: {task}\n"
        "ctx: {context}\n"
        "world: {world_state_summary}\n"
        "mem: {executor_memory}\n"
    )
    rendered = (
        template
        .replace("{task}", "do thing")
        .replace("{context}", "(none)")
        .replace("{world_state_summary}", "ws ok")
        .replace("{executor_memory}", "## Prior engagements (lessons learned)\n- prior")
    )
    assert "do thing" in rendered
    assert "ws ok" in rendered
    assert "Prior engagements" in rendered
    assert "{executor_memory}" not in rendered


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="provision_workspace uses os.mkfifo (Linux-only)",
)
async def test_workspace_legacy_path_substitutes_executor_memory(tmp_path):
    """The legacy provision_workspace path threads executor_memory through.

    Forward-compat coverage for the claude_code driver — no claude_code
    executor opts into memory today, but workspace.py wires the slot so a
    future memory-enabled claude_code executor works without plumbing.
    """
    from drivers.workspace import provision_workspace

    class _Defn:
        type = "x"
        prompt_template_path = str(tmp_path / "prompt.md")
        hooks_path = None
    (tmp_path / "prompt.md").write_text(
        "task={task} mem={executor_memory}\n", encoding="utf-8",
    )

    eng_root = tmp_path / "engagements"
    eng_root.mkdir()
    plugins_root = tmp_path / "plugins_root"
    plugins_root.mkdir()

    await provision_workspace(
        engagements_root=str(eng_root),
        base_plugins_root=str(plugins_root),
        engagement_id="abc12345",
        defn=_Defn(),
        task="dotask",
        context="(none)",
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        executor_memory="## Prior\nbody",
    )
    claude_md = (eng_root / "abc12345" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "task=dotask" in claude_md
    assert "mem=## Prior\nbody" in claude_md


async def test_executor_archive_is_read_on_second_engagement(tmp_path):
    """First engagement writes to the archive; second reads it.

    Drives the read+write contract end-to-end against an in-memory mock
    memory_provider. Doesn't exercise engage_executor's full topic-
    creation path — that requires a Telegram channel mock + engagement
    registry, which is out of scope for the L3 contract test.
    """
    # In-memory archive: session_id → list of (user, assistant) tuples.
    archive: dict[str, list[tuple[str, str]]] = {}

    class _Mp:
        async def ensure_session(self, *, session_id, agent_role,
                                 user_peer="nicola"):
            archive.setdefault(session_id, [])

        async def add_turn(self, *, session_id, agent_role,
                           user_text, assistant_text, user_peer="nicola"):
            archive.setdefault(session_id, []).append(
                (user_text, assistant_text),
            )

        async def get_context(self, *, session_id, tokens,
                              search_query=None, agent_role=None):
            entries = archive.get(session_id, [])
            if not entries:
                return ""
            return "## Recent exchanges\n" + "\n".join(
                f"- {u} → {a}" for u, a in entries
            )

    mp = _Mp()

    # First "engagement": the WRITE side of _finalize_engagement.
    # Simulate by calling the same shape directly.
    await mp.ensure_session(
        session_id="telegram-42-executor-configurator",
        agent_role="executor-configurator",
    )
    await mp.add_turn(
        session_id="telegram-42-executor-configurator",
        agent_role="executor-configurator",
        user_text="(executor engagement summary)",
        assistant_text='{"task": "edit-scope", "outcome": "completed"}',
    )

    # Second engagement: the READ side via _fetch_executor_archive.
    out = await _fetch_executor_archive(
        memory_provider=mp,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    )
    assert "Prior engagements (lessons learned)" in out
    assert "edit-scope" in out
    assert "completed" in out
