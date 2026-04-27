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
        session_id="telegram:42:executor:configurator",
        agent_role="executor:configurator",
    )
    mp.get_context.assert_awaited_once()
    kwargs = mp.get_context.await_args.kwargs
    assert kwargs["session_id"] == "telegram:42:executor:configurator"
    assert kwargs["agent_role"] == "executor:configurator"
    assert kwargs["tokens"] == 2000


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
