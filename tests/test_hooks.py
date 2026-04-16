"""Tests for hooks.py -- safety hooks."""

import pytest

from hooks import block_dangerous_commands, make_path_scope_hook

pytestmark = pytest.mark.asyncio


# The SDK passes {"signal": None} as context. Hooks must not rely on it.
CTX: dict = {"signal": None}


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


# ------------------------------------------------------------------
# block_dangerous_commands
# ------------------------------------------------------------------


async def test_block_rm_rf():
    data = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    result = await block_dangerous_commands(data, "tid-1", CTX)
    assert result is not None
    assert _decision(result) == "deny"
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


async def test_allow_safe_command():
    data = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}
    result = await block_dangerous_commands(data, "tid-2", CTX)
    assert result is None


async def test_skip_non_bash_tools():
    data = {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}
    result = await block_dangerous_commands(data, "tid-3", CTX)
    assert result is None


async def test_block_ssh():
    data = {"tool_name": "Bash", "tool_input": {"command": "ssh root@192.168.1.1"}}
    result = await block_dangerous_commands(data, "tid-4", CTX)
    assert result is not None
    assert _decision(result) == "deny"


async def test_block_scp():
    data = {"tool_name": "Bash", "tool_input": {"command": "scp f user@h:/tmp/"}}
    result = await block_dangerous_commands(data, "tid-5", CTX)
    assert result is not None


async def test_block_shutdown():
    data = {"tool_name": "Bash", "tool_input": {"command": "shutdown -h now"}}
    result = await block_dangerous_commands(data, "tid-6", CTX)
    assert result is not None


async def test_block_curl_post():
    data = {"tool_name": "Bash", "tool_input": {"command": "curl -X POST http://x"}}
    result = await block_dangerous_commands(data, "tid-7", CTX)
    assert result is not None


# ------------------------------------------------------------------
# make_path_scope_hook
# ------------------------------------------------------------------


async def test_path_allowed_read_for_assistant():
    hook = make_path_scope_hook("assistant")
    data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "addon_configs/casa/agents/ellen.yaml"},
    }
    assert await hook(data, "tid-10", CTX) is None


async def test_path_denied_write_for_assistant_to_config():
    hook = make_path_scope_hook("assistant")
    data = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/config/configuration.yaml"},
    }
    result = await hook(data, "tid-11", CTX)
    assert result is not None
    assert _decision(result) == "deny"
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


async def test_path_traversal_blocked():
    hook = make_path_scope_hook("butler")
    data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "workspace/../../etc/passwd"},
    }
    result = await hook(data, "tid-12", CTX)
    assert result is not None
    assert _decision(result) == "deny"


async def test_path_allowed_write_workspace_for_assistant():
    hook = make_path_scope_hook("assistant")
    data = {"tool_name": "Write", "tool_input": {"file_path": "workspace/notes.txt"}}
    assert await hook(data, "tid-13", CTX) is None


async def test_path_allowed_write_workspace_for_plugin_builder():
    hook = make_path_scope_hook("plugin-builder")
    data = {"tool_name": "Write", "tool_input": {"file_path": "workspace/plugin.py"}}
    assert await hook(data, "tid-13b", CTX) is None


async def test_butler_cannot_write_workspace():
    hook = make_path_scope_hook("butler")
    data = {"tool_name": "Write", "tool_input": {"file_path": "workspace/notes.txt"}}
    result = await hook(data, "tid-13c", CTX)
    assert result is not None
    assert _decision(result) == "deny"


async def test_unknown_role_allowed():
    """Unknown roles fall through to allow. Phase 2 may invert this."""
    hook = make_path_scope_hook("unknown-role")
    data = {"tool_name": "Read", "tool_input": {"file_path": "/anywhere/file.txt"}}
    assert await hook(data, "tid-14", CTX) is None


async def test_empty_role_allowed():
    """Missing role (empty string) also falls through to allow."""
    hook = make_path_scope_hook("")
    data = {"tool_name": "Read", "tool_input": {"file_path": "/anywhere/file.txt"}}
    assert await hook(data, "tid-14b", CTX) is None


async def test_non_file_tool_skipped():
    hook = make_path_scope_hook("butler")
    data = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert await hook(data, "tid-15", CTX) is None
