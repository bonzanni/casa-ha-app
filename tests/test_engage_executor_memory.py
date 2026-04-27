"""Unit coverage for _fetch_executor_archive (M4 L3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import _fetch_executor_archive


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_returns_empty_when_provider_none():
    out = _run(_fetch_executor_archive(
        memory_provider=None,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    ))
    assert out == ""


def test_returns_empty_when_archive_empty():
    mp = MagicMock()
    mp.ensure_session = AsyncMock(return_value=None)
    mp.get_context = AsyncMock(return_value="")

    out = _run(_fetch_executor_archive(
        memory_provider=mp,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    ))
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


def test_returns_wrapped_block_when_archive_populated():
    mp = MagicMock()
    mp.ensure_session = AsyncMock(return_value=None)
    mp.get_context = AsyncMock(
        return_value="## Recent exchanges\n- prior task done\n",
    )

    out = _run(_fetch_executor_archive(
        memory_provider=mp,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    ))
    assert out.startswith("## Prior engagements (lessons learned)\n")
    assert "prior task done" in out


def test_returns_empty_when_provider_raises():
    mp = MagicMock()
    mp.ensure_session = AsyncMock(side_effect=RuntimeError("honcho boom"))
    mp.get_context = AsyncMock(return_value="never reached")

    out = _run(_fetch_executor_archive(
        memory_provider=mp,
        channel="telegram", chat_id="42",
        executor_type="configurator", token_budget=2000,
    ))
    assert out == ""
