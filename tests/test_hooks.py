"""Tests for hooks.py -- safety hooks."""

import pytest

from hooks import block_dangerous_commands

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
# make_path_scope_hook_v2 — parameterized path scope
# ------------------------------------------------------------------


async def test_v2_writable_allows_write_under_prefix():
    from hooks import make_path_scope_hook_v2
    hook = make_path_scope_hook_v2(
        writable=["/addon_configs/casa-agent/workspace"],
        readable=["/addon_configs/casa-agent/workspace"],
    )
    data = {"tool_name": "Write",
            "tool_input": {"file_path":
                "/addon_configs/casa-agent/workspace/note.txt"}}
    assert await hook(data, "tid-20", CTX) is None


async def test_v2_writable_denies_write_outside():
    from hooks import make_path_scope_hook_v2
    hook = make_path_scope_hook_v2(
        writable=["/addon_configs/casa-agent/workspace"],
        readable=[],
    )
    data = {"tool_name": "Write",
            "tool_input": {"file_path": "/etc/shadow"}}
    result = await hook(data, "tid-21", CTX)
    assert result is not None
    assert _decision(result) == "deny"


async def test_v2_readable_allows_read():
    from hooks import make_path_scope_hook_v2
    hook = make_path_scope_hook_v2(
        writable=[],
        readable=["/addon_configs"],
    )
    data = {"tool_name": "Read",
            "tool_input": {"file_path": "/addon_configs/casa-agent/agents/butler/character.yaml"}}
    assert await hook(data, "tid-22", CTX) is None


async def test_v2_traversal_blocked():
    from hooks import make_path_scope_hook_v2
    hook = make_path_scope_hook_v2(
        writable=[],
        readable=["/addon_configs"],
    )
    data = {"tool_name": "Read",
            "tool_input": {"file_path": "/addon_configs/../etc/passwd"}}
    result = await hook(data, "tid-23", CTX)
    assert result is not None
    assert _decision(result) == "deny"


async def test_v2_non_file_tool_skipped():
    from hooks import make_path_scope_hook_v2
    hook = make_path_scope_hook_v2(writable=[], readable=[])
    data = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert await hook(data, "tid-24", CTX) is None
