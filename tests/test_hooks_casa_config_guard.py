"""Tests for casa_config_guard hook policy (Plan 3)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestCasaConfigGuard:
    async def test_blocks_write_to_data(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/data", "/addon_configs/casa-agent/schema"],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": "/data/x.json"}},
            None, {},
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_blocks_write_to_schema(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/addon_configs/casa-agent/schema"],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "/addon_configs/casa-agent/schema/x.json"}},
            None, {},
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_allows_regular_write(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=["/data"],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": "/addon_configs/casa-agent/agents/specialists/x/character.yaml"}},
            None, {},
        )
        assert out is None

    async def test_blocks_bash_rm_resident(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=[],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /addon_configs/casa-agent/agents/butler"}},
            None, {},
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_allows_bash_rm_specialist(self):
        from hooks import make_casa_config_guard_hook
        hook = make_casa_config_guard_hook(
            forbid_write_paths=[],
            forbid_delete_residents=True,
        )
        out = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /addon_configs/casa-agent/agents/specialists/fitness"}},
            None, {},
        )
        assert out is None

    async def test_registered_in_hook_policies(self):
        from hooks import HOOK_POLICIES
        assert "casa_config_guard" in HOOK_POLICIES
