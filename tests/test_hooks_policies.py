"""Tests for HOOK_POLICIES registry + resolve_hooks."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


CTX: dict = {"signal": None}


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


class TestPathScopeV2:
    async def test_writable_allows_write_under_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=["/addon_configs/casa-agent/workspace"],
            readable=["/addon_configs/casa-agent/workspace"],
        )
        data = {"tool_name": "Write",
                "tool_input": {"file_path":
                    "/addon_configs/casa-agent/workspace/note.txt"}}
        assert await hook(data, "tid", CTX) is None

    async def test_writable_denies_write_outside_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=["/addon_configs/casa-agent/workspace"],
            readable=["/addon_configs/casa-agent/workspace"],
        )
        data = {"tool_name": "Write",
                "tool_input": {"file_path": "/etc/shadow"}}
        result = await hook(data, "tid", CTX)
        assert result is not None and _decision(result) == "deny"

    async def test_readable_allows_read_under_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=[],
            readable=["/addon_configs"],
        )
        data = {"tool_name": "Read",
                "tool_input": {"file_path": "/addon_configs/something"}}
        assert await hook(data, "tid", CTX) is None

    async def test_readable_denies_read_outside_prefix(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(writable=[], readable=["/data"])
        data = {"tool_name": "Read",
                "tool_input": {"file_path": "/etc/passwd"}}
        result = await hook(data, "tid", CTX)
        assert result is not None and _decision(result) == "deny"

    async def test_traversal_normalized(self):
        from hooks import make_path_scope_hook_v2
        hook = make_path_scope_hook_v2(
            writable=[], readable=["/addon_configs"],
        )
        data = {"tool_name": "Read",
                "tool_input": {"file_path":
                    "/addon_configs/../etc/passwd"}}
        result = await hook(data, "tid", CTX)
        assert result is not None and _decision(result) == "deny"


class TestResolveHooks:
    def test_empty_hooks_config_resolves_empty_dict(self):
        from hooks import resolve_hooks
        from config import HooksConfig

        resolved = resolve_hooks(HooksConfig(), default_cwd="/cwd")
        # Default is block_dangerous_bash + path_scope scoped to cwd.
        assert "PreToolUse" in resolved
        assert len(resolved["PreToolUse"]) == 2

    def test_explicit_block_dangerous_bash(self):
        from hooks import resolve_hooks
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[{"policy": "block_dangerous_bash"}])
        resolved = resolve_hooks(cfg, default_cwd="/cwd")
        assert "PreToolUse" in resolved
        # Just one matcher when only block_dangerous_bash is listed.
        assert len(resolved["PreToolUse"]) == 1

    def test_explicit_path_scope_with_params(self):
        from hooks import resolve_hooks
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[
            {"policy": "path_scope",
             "writable": ["/workspace"],
             "readable": ["/workspace", "/addon_configs"]},
        ])
        resolved = resolve_hooks(cfg, default_cwd="/cwd")
        assert len(resolved["PreToolUse"]) == 1

    def test_unknown_policy_raises(self):
        from hooks import resolve_hooks, UnknownPolicyError
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[{"policy": "not_a_real_policy"}])
        with pytest.raises(UnknownPolicyError, match="not_a_real_policy"):
            resolve_hooks(cfg, default_cwd="/cwd")

    def test_path_scope_bad_params_raises(self):
        """Unknown parameters on a policy surface as load error."""
        from hooks import resolve_hooks, UnknownPolicyError
        from config import HooksConfig

        cfg = HooksConfig(pre_tool_use=[
            {"policy": "path_scope", "bogus_param": []},
        ])
        with pytest.raises(UnknownPolicyError, match="bogus_param"):
            resolve_hooks(cfg, default_cwd="/cwd")
