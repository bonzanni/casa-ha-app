"""Tests for commit_size_guard hook policy (Plan 3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


class TestCommitSizeGuard:
    async def test_below_threshold_allows(self):
        from hooks import make_commit_size_guard_hook
        hook = make_commit_size_guard_hook(max_files=20)
        with patch("hooks._git_porcelain_count", return_value=5):
            out = await hook(
                {"tool_name": "Write", "tool_input": {"file_path": "/addon_configs/casa-agent/agents/x.yaml"}},
                None, {},
            )
        assert out is None

    async def test_above_threshold_denies(self):
        from hooks import make_commit_size_guard_hook
        hook = make_commit_size_guard_hook(max_files=20)
        with patch("hooks._git_porcelain_count", return_value=25):
            out = await hook(
                {"tool_name": "Write", "tool_input": {"file_path": "/addon_configs/casa-agent/agents/x.yaml"}},
                None, {},
            )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "25" in out["hookSpecificOutput"]["permissionDecisionReason"]

    async def test_ignores_non_write_tools(self):
        from hooks import make_commit_size_guard_hook
        hook = make_commit_size_guard_hook(max_files=1)
        out = await hook(
            {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
            None, {},
        )
        assert out is None

    async def test_registered(self):
        from hooks import HOOK_POLICIES
        assert "commit_size_guard" in HOOK_POLICIES
