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


# ------------------------------------------------------------------
# Bug 1 (v0.14.6) regression suite — block_dangerous_commands must
# catch every flag-equivalent of `rm -rf` plus wrapper-shell variants.
# Pre-fix the regex `\brm\s+-rf\b` was bypassed by all of these, which
# was verified live on N150 production.
# ------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "rm -rf /data",
    "rm -r -f /data",
    "rm -f -r /data",
    "rm -R -f /data",
    "rm -fR /data",
    "rm -rfv /data",
    "rm -rfd /data",
    "rm --recursive --force /data",
    "rm --force --recursive /data",
    "rm --force -r /data",
    "rm -r --force /data",
    "  rm  -r   -f   /data  ",                # extra whitespace
    "rm -rf '/data with space'",              # quoted positional
    "rm -rf -- /data",                        # -- separator
])
async def test_rm_recursive_force_all_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-rm", CTX)
    assert result is not None, f"BYPASS: {cmd!r}"
    assert _decision(result) == "deny"


@pytest.mark.parametrize("cmd", [
    "rm -i /tmp/foo",                          # interactive, no recursive
    "rm /tmp/foo",                             # plain rm
    "rm -r /tmp/foo",                          # recursive only, no force
    "rm -f /tmp/foo",                          # force only, no recursive
    "ls -la /",                                # unrelated
    "echo rm -rf /data",                       # echo as argv[0]
])
async def test_safe_rm_variants_allowed(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-rm-safe", CTX)
    assert result is None, f"unexpectedly blocked: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "true && rm -rf /data",
    "false || rm -rf /data",
    "ls; rm -rf /data",
    "cat foo | rm -rf /data",
    "echo & rm -rf /data",
])
async def test_pipeline_separators_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-pipe", CTX)
    assert result is not None, f"pipeline bypass: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    'bash -c "rm -rf /data"',
    "bash -c 'rm -r -f /data'",
    "sh -c 'rm --recursive --force /data'",
    'sh -c "ls; rm -rf /data"',
])
async def test_wrapper_shell_recursion_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-wrap", CTX)
    assert result is not None, f"wrapper-shell bypass: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "/usr/bin/rm -rf /data",                   # absolute path to rm
    "/bin/rm -rfv /data",
    "/usr/bin/shutdown",
])
async def test_absolute_path_program_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-abs", CTX)
    assert result is not None, f"absolute-path bypass: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "shutdown -h now",
    "reboot",
    "halt",
    "poweroff",
    "ssh root@host",
    "scp f user@host:/tmp/",
])
async def test_other_denied_programs(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-other", CTX)
    assert result is not None, f"should be denied: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "dd if=/dev/zero of=/dev/sda",
    "dd if=/dev/sda of=/tmp/disk.img",
])
async def test_dd_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-dd", CTX)
    assert result is not None


@pytest.mark.parametrize("cmd", [
    "curl -X POST http://x",
    "curl -XPOST http://x",
    "curl --request POST http://x",
    "curl -d 'k=v' http://x",
    "curl --data '{}' http://x",
    "curl --data-binary @file http://x",
    "curl -T file.txt sftp://host/",
])
async def test_curl_write_methods_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-curl", CTX)
    assert result is not None, f"curl write bypass: {cmd!r}"


async def test_curl_get_allowed():
    """Plain GET requests are not blocked by this hook."""
    data = {"tool_name": "Bash",
            "tool_input": {"command": "curl https://example.com/api"}}
    assert await block_dangerous_commands(data, "tid-curl-get", CTX) is None


async def test_malformed_quotes_does_not_crash():
    """Mismatched quotes fall back to whitespace split, no exception leaks."""
    data = {"tool_name": "Bash",
            "tool_input": {"command": "rm -rf 'unterminated"}}
    result = await block_dangerous_commands(data, "tid-malformed", CTX)
    # Best-effort fallback should still catch this case.
    assert result is not None


async def test_env_var_assignment_skipped_to_argv0():
    """FOO=bar rm -rf / -- the scanner should look past env assignments."""
    data = {"tool_name": "Bash",
            "tool_input": {"command": "FOO=bar BAR=baz rm -rf /tmp/x"}}
    result = await block_dangerous_commands(data, "tid-envassign", CTX)
    assert result is not None
