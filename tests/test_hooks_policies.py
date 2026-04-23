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


# --- 0.13.1 — two-tier HOOK_POLICIES shape --------------------------------


def test_hook_policies_are_two_tier_dicts():
    """Each policy entry is {'matcher': regex, 'factory': callable}."""
    from hooks import HOOK_POLICIES

    for name, entry in HOOK_POLICIES.items():
        assert isinstance(entry, dict), f"{name}: must be dict"
        assert set(entry.keys()) == {"matcher", "factory"}, (
            f"{name}: keys must be exactly {{matcher, factory}}"
        )
        assert isinstance(entry["matcher"], str)
        assert callable(entry["factory"])


def test_factory_returns_hookcallback():
    """factory(**kwargs) must return an awaitable-callable, not a HookMatcher."""
    from hooks import HOOK_POLICIES

    entry = HOOK_POLICIES["casa_config_guard"]
    cb = entry["factory"](forbid_write_paths=["/data"])

    # HookCallback is (input, tool_use_id, context) -> Awaitable[dict | None]
    # We don't import HookCallback directly (it's a type alias); we just
    # assert callability + coroutinefunction-ness.
    import inspect
    assert inspect.iscoroutinefunction(cb), (
        f"factory returned {type(cb)!r}; must be async function"
    )


def test_resolve_hooks_still_builds_HookMatcher():
    """The SDK-path consumer (resolve_hooks) still produces HookMatcher objects."""
    from config import HooksConfig
    from hooks import resolve_hooks

    cfg = HooksConfig(pre_tool_use=[{"policy": "block_dangerous_bash"}])
    resolved = resolve_hooks(cfg, default_cwd="/tmp")
    assert "PreToolUse" in resolved
    assert len(resolved["PreToolUse"]) == 1
    # HookMatcher has .matcher and .hooks — duck-type check is fine.
    matcher = resolved["PreToolUse"][0]
    assert hasattr(matcher, "matcher") and hasattr(matcher, "hooks")
    assert matcher.matcher == "Bash"
