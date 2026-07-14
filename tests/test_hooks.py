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
    assert result == {}


async def test_block_bash_write_to_plugins_registry():
    """Sol #5: block_dangerous_bash also denies a Bash write into /config/plugins
    (path_scope ignores Bash) — closes the claude_code executor HTTP-hook path."""
    data = {"tool_name": "Bash",
            "tool_input": {"command": "echo x > /config/plugins/registry.json"}}
    result = await block_dangerous_commands(data, "tid-plugins", CTX)
    assert _decision(result) == "deny"


async def test_allow_bash_read_of_plugins_store():
    """Reading the store (the plugin-developer's --plugin-dir'd content) is fine."""
    data = {"tool_name": "Bash",
            "tool_input": {"command": "cat /config/plugins/store/x/y/plugin.json"}}
    result = await block_dangerous_commands(data, "tid-plug-read", CTX)
    assert result == {}


async def test_skip_non_bash_tools():
    data = {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}
    result = await block_dangerous_commands(data, "tid-3", CTX)
    assert result == {}


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
    assert await hook(data, "tid-20", CTX) == {}


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
    assert await hook(data, "tid-22", CTX) == {}


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
    assert await hook(data, "tid-24", CTX) == {}


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
    assert result == {}, f"unexpectedly blocked: {cmd!r}"


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
    assert await block_dangerous_commands(data, "tid-curl-get", CTX) == {}


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


# ------------------------------------------------------------------
# H8 (v0.50.0): newline is a shell command separator equivalent to ';'.
# Pre-fix _PIPELINE_SPLIT_RE never split on newlines, so the second line's
# program landed in argv[1:] of the first and was never inspected as argv[0].
# Per-test `unit` marker because the module-level pytestmark lacks it.
# ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "cd /tmp\nrm -rf /config",            # LF separator
    "cd /tmp\r\nrm -rf /config",          # CRLF separator
    "cd /tmp\nrm -rf /config\n",          # trailing newline
    "echo done\nssh host uptime",         # deny-listed program on line 2
    "true\ncurl -X POST http://x",        # curl-data check on line 2
    "cd /tmp\ndd if=/dev/zero of=/dev/sda",
])
async def test_newline_separated_commands_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-nl", CTX)
    assert result is not None and result != {}, f"newline bypass: {cmd!r}"
    assert _decision(result) == "deny"


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "curl \\\n -X POST http://x",   # backslash continuation: one logical command
    "rm \\\n -rf /data",
])
async def test_backslash_continuation_still_blocked(cmd):
    """Line continuations are collapsed before newline splitting, else the
    flag tokens land in a separate piece and evade the checks."""
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-cont", CTX)
    assert result is not None and result != {}, f"continuation bypass: {cmd!r}"


@pytest.mark.unit
async def test_multiline_safe_command_allowed():
    data = {"tool_name": "Bash", "tool_input": {"command": "ls /tmp\necho ok"}}
    assert await block_dangerous_commands(data, "tid-nl-safe", CTX) == {}


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "ls # note\nrm -rf /data",   # '#' must not swallow the next line's rm
    "echo ok #x\nssh root@host",
])
async def test_comment_does_not_hide_next_line_command(cmd):
    """A '#' comment ends at the newline; the quote-aware tokenizer must not
    treat '#' as a comment that swallows the (newline-collapsed) rest of the
    command and hides a dangerous program on the following line."""
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-comment", CTX)
    assert result is not None and result != {}, f"comment bypass: {cmd!r}"
    assert _decision(result) == "deny"


# ------------------------------------------------------------------
# L13 (v0.50.0): the pipeline split must be quote-aware. Operators inside
# quoted strings are data, not pipeline boundaries — pre-fix the raw-regex
# split cut the quote in half and the dangling piece falsely denied.
# ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    'git commit -m "cleanup && rm -rf handling"',   # && inside double quotes
    'echo "done; ssh keys rotated"',                # ; inside double quotes
    "git commit -m 'fix: reboot | shutdown docs'",  # | inside single quotes
    'echo "a & b" && ls',                           # quoted & plus a real separator
])
async def test_quoted_operators_are_not_false_positives(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-quoted", CTX)
    assert result == {}, f"benign quoted command falsely denied: {cmd!r}"


@pytest.mark.unit
async def test_split_pipeline_is_quote_aware():
    from hooks import _split_pipeline
    assert _split_pipeline('git commit -m "a && rm -rf b"') == [
        ["git", "commit", "-m", "a && rm -rf b"]
    ]
    # real separators still split
    assert _split_pipeline("true && rm -rf /data") == [["true"], ["rm", "-rf", "/data"]]
    # redirection target must not become argv[0] of a new piece
    assert _split_pipeline("echo hi > /tmp/ssh") == [["echo", "hi", "/tmp/ssh"]]


# ------------------------------------------------------------------
# v0.50.0 security-review must-fix (M16 root cause): substitution and
# exec wrappers hand their payload to a shell without the dangerous
# program ever appearing as argv[0] of a pipeline piece. Pre-fix,
# `echo $(rm -rf /)`, backticks, `eval "rm -rf /"` and
# `... | xargs rm -rf` all reached dangerous exec undetected.
# ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "echo $(rm -rf /)",                # command substitution as an argument
    "$(rm -rf /)",                     # bare command substitution
    'echo "$(rm -rf /data)"',          # double-quoted substitution still executes
    "x=`rm -rf /`",                    # backticks in an assignment
    "`rm -rf /`",                      # bare backticks
    'eval "rm -rf /"',                 # eval with double-quoted payload
    "eval 'rm -rf /'",                 # eval with single-quoted payload
    "echo / | xargs rm -rf",           # xargs execs rm with stdin-derived args
    "find /data | xargs -n 1 rm -rf",  # xargs with a separate-argument flag
    "true; x=$(ssh root@host id)",     # deny-listed program inside substitution
])
async def test_substitution_and_exec_wrappers_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-subst", CTX)
    assert result is not None and result != {}, f"substitution bypass: {cmd!r}"
    assert _decision(result) == "deny"


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "awk '{print $(NF-1)}' file.txt",      # awk field expr looks like $(...)
    "echo `date`",                          # benign backtick substitution
    'eval "$(ssh-agent -s)"',               # classic eval; inner prog is safe
    "ls | xargs -n1 echo",                  # xargs wrapping a safe program
    "find . -name '*.pyc' | xargs rm -f",   # force-only rm stays allowed
    "echo $((1+2))",                        # arithmetic expansion, not a command
])
async def test_substitution_benign_allowed(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-subst-ok", CTX)
    assert result == {}, f"benign substitution falsely denied: {cmd!r}"


# ------------------------------------------------------------------
# v0.50.0 hardening round 2 (security-review follow-up): two in-scope
# bypass classes in the argv scanner.
# (1) bash's '|&' (pipe stdout+stderr) and the case-branch terminators
#     ';&' / ';;&' are emitted by shlex's punctuation-run tokenizer as
#     single tokens that were NOT in _PIPELINE_SEPARATORS, so the two
#     stages merged and the RHS program never became argv[0].
# (2) exec-wrapper prefixes (nohup, timeout, env, sudo, ...) run their
#     tail as the real command; argv[0] was the benign wrapper and the
#     tail was never re-scanned.
# ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "echo x |& rm -rf /",          # pipe-both: RHS is a new simple command
    "echo x|&rm -rf /",            # no-space spelling
    "true ;& rm -rf /data",        # case fall-through terminator
    "true ;;& rm -rf /data",       # case continue-matching terminator
    "echo x |& ssh root@host",     # deny-listed program on the RHS
])
async def test_pipe_both_and_case_terminator_separators_blocked(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-pipeboth", CTX)
    assert result is not None and result != {}, f"separator bypass: {cmd!r}"
    assert _decision(result) == "deny"


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "nohup rm -rf /data",
    "timeout 5 rm -rf /",
    "timeout -k 3 5s rm -rf /data",
    "timeout --signal=KILL 5 rm -rf /data",
    "env rm -rf /data",
    "env A=B rm -rf /data",
    "env -i A=B rm -rf /data",
    "env -u PATH rm -rf /data",
    "stdbuf -oL rm -rf /data",
    "setsid rm -rf /data",
    "time rm -rf /data",
    "nice rm -rf /data",
    "nice -n 5 rm -rf /data",
    "nice -n -5 rm -rf /data",
    "ionice -c 3 rm -rf /data",
    "chrt 50 rm -rf /data",
    "chrt -f 50 rm -rf /data",
    "taskset 0x1 rm -rf /data",
    "taskset -c 0-3 rm -rf /data",
    "unbuffer rm -rf /data",
    "sudo rm -rf /data",
    "sudo -u root rm -rf /data",
    "sudo -- rm -rf /data",
    "doas rm -rf /data",
    "doas -u root rm -rf /data",
    "/usr/bin/nohup rm -rf /data",      # absolute-path wrapper
    "sudo nohup rm -rf /data",          # nested wrappers
    "nohup timeout 5 rm -rf /",         # nested + duration positional
    "nohup ssh root@host",              # deny-listed program behind wrapper
    "timeout 5 curl -X POST http://x",  # curl-data check behind wrapper
    "ls | nohup rm -rf /data",          # wrapper as a later pipeline stage
])
async def test_exec_wrapper_prefixes_unwrapped(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-wrapper", CTX)
    assert result is not None and result != {}, f"wrapper bypass: {cmd!r}"
    assert _decision(result) == "deny"


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "nohup python server.py",
    "timeout 5 curl https://example.com",   # GET behind a wrapper stays allowed
    "env FOO=bar make build",
    "nice -n 10 tar -xf a.tar",
    "sudo -u casa ls /data",
    "time make test",
    "timeout 5 rm -r /tmp/x",               # recursive-only rm stays allowed
    "env",                                  # bare wrapper, no tail
])
async def test_exec_wrapper_benign_tail_allowed(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-wrapper-ok", CTX)
    assert result == {}, f"benign wrapper falsely denied: {cmd!r}"


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    "echo hi > /tmp/out",
    "echo hi >> /tmp/out",
    "sort < /tmp/in",
    "echo hi 2>&1",
    "ls &> /tmp/out",        # redirect-both is a REDIRECTION, not a pipe
    "echo hi >& /tmp/out",
])
async def test_true_redirections_do_not_split_or_deny(cmd):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    result = await block_dangerous_commands(data, "tid-redir", CTX)
    assert result == {}, f"redirection falsely denied: {cmd!r}"


@pytest.mark.unit
async def test_split_pipeline_pipe_both_and_case_terminators():
    from hooks import _split_pipeline
    assert _split_pipeline("echo x |& rm -rf /") == [
        ["echo", "x"], ["rm", "-rf", "/"]
    ]
    assert _split_pipeline("a ;& b") == [["a"], ["b"]]
    assert _split_pipeline("a ;;& b") == [["a"], ["b"]]
    # True redirections stay merged: '&>' / '>&' are redirections
    # ('|&' is pipe-both — NOT a redirection).
    assert _split_pipeline("ls &> /tmp/o") == [["ls", "/tmp/o"]]
    assert _split_pipeline("echo hi 2>&1") == [["echo", "hi", "2", "1"]]


# ------------------------------------------------------------------
# _normalize_path — L-2 (v0.34.2): single-slash absolute paths
# ------------------------------------------------------------------


class TestNormalizePathSingleSlash:
    """L-2 (v0.34.2): _normalize_path must not produce double leading slash."""

    def test_preserves_single_leading_slash(self):
        from hooks import _normalize_path
        assert _normalize_path("/addon_configs/foo") == "/addon_configs/foo"

    def test_handles_dotdot_at_root(self):
        from hooks import _normalize_path
        assert _normalize_path("/foo/../bar") == "/bar"

    def test_handles_relative_path_unchanged(self):
        from hooks import _normalize_path
        assert _normalize_path("foo/bar") == "foo/bar"


# ------------------------------------------------------------------
# v0.50.0 security-review must-fix: _normalize_path must collapse
# redundant slashes. PurePosixPath preserves a POSIX-special leading
# '//' as a distinct root, so '//config/agents/ellen' normalized to
# the malformed '///config/agents/ellen' and slipped past every
# prefix check (casa_config_guard forbid_write + _deletes_resident),
# even though the Linux kernel resolves '//' as '/'.
# ------------------------------------------------------------------


@pytest.mark.unit
class TestNormalizePathSlashCollapse:
    async def test_double_leading_slash_collapsed(self):
        from hooks import _normalize_path
        assert _normalize_path("//x") == "/x"

    async def test_triple_leading_slash_collapsed(self):
        from hooks import _normalize_path
        assert _normalize_path("///config/agents/ellen") == "/config/agents/ellen"

    async def test_leading_and_interior_double_slash_collapsed(self):
        from hooks import _normalize_path
        assert _normalize_path("//config/agents//ellen") == "/config/agents/ellen"

    async def test_collapse_composes_with_dotdot(self):
        from hooks import _normalize_path
        assert _normalize_path("//config//agents/x/../ellen") == "/config/agents/ellen"


# ------------------------------------------------------------------
# v0.37.2 (C-1): engagement_permission_relay wired into casa_core
# ------------------------------------------------------------------


class TestEngagementPermissionRelayWired:
    """v0.37.2 (C-1): the relay policy is wired into casa_core's built
    cc_hook_policies dict via ``_wire_engagement_permission_relay``.

    The wiring helper is invoked from ``casa_core.main()`` at two sites
    (public-8099 fallback + internal-socket). The smoke verifies the
    helper produces a dict entry shaped correctly: name +
    ``.*`` matcher + an awaitable callback.
    """

    def test_policy_registered_under_correct_name_and_matcher(self):
        from unittest.mock import MagicMock
        from casa_core import _wire_engagement_permission_relay

        policies: dict = {}
        _wire_engagement_permission_relay(
            policies,
            engagement_registry=MagicMock(),
            telegram_channel=MagicMock(),
        )

        assert "engagement_permission_relay" in policies
        matcher, cb = policies["engagement_permission_relay"]
        assert matcher == r".*"
        assert callable(cb)

    def test_wiring_uses_shared_permission_queues(self):
        """v0.75.0 (W5/Sol B3,B4): the operator verdict now flows through
        ``verdict_broker.BROKER``, not this queue dict — the relay accepts
        ``queues=`` as a deprecated, accepted-and-ignored kwarg (see
        ``channel_handlers.PERMISSION_QUEUES`` docstring) so mid-migration
        callers don't crash. This just pins the public alias to the same
        underlying dict until every wiring site drops the parameter."""
        from channels.channel_handlers import (
            PERMISSION_QUEUES,
            _PERMISSION_QUEUES,
        )

        assert PERMISSION_QUEUES is _PERMISSION_QUEUES


# ------------------------------------------------------------------
# I-2 (v0.69.8): agent-home settings.json self-grant guard
# ------------------------------------------------------------------


async def test_settings_guard_blocks_write_to_agent_settings():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "Write", "tool_input": {
        "file_path": "/config/agent-home/assistant/.claude/settings.json"}}
    result = await hook(data, "t", CTX)
    assert _decision(result) == "deny"


async def test_settings_guard_blocks_edit_and_multiedit():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    for tool in ("Edit", "MultiEdit"):
        data = {"tool_name": tool, "tool_input": {
            "file_path": "/config/agent-home/butler/.claude/settings.json"}}
        assert _decision(await hook(data, "t", CTX)) == "deny"


async def test_settings_guard_blocks_traversal_to_settings():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "Write", "tool_input": {
        "file_path": "/config/agent-home/assistant/skills/../.claude/settings.json"}}
    assert _decision(await hook(data, "t", CTX)) == "deny"


async def test_settings_guard_allows_other_writes():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "Write", "tool_input": {
        "file_path": "/config/agent-home/assistant/notes.md"}}
    assert await hook(data, "t", CTX) == {}


async def test_plugin_guard_blocks_write_to_registry():
    """§3.11/§3.13: direct writes to the plugin registry are denied."""
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    for path in ("/config/plugins/registry.json",
                 "/config/plugins/store/superpowers/abc/skill.md"):
        data = {"tool_name": "Write", "tool_input": {"file_path": path}}
        assert _decision(await hook(data, "t", CTX)) == "deny"


async def test_plugin_guard_allows_sibling_path():
    """A sibling like /config/plugins-notes.md is NOT under the guarded dir."""
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "Write", "tool_input": {
        "file_path": "/config/plugins-notes.md"}}
    assert await hook(data, "t", CTX) == {}


async def test_plugin_guard_blocks_traversal_and_bash():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    trav = {"tool_name": "Edit", "tool_input": {
        "file_path": "/config/agents/../plugins/registry.json"}}
    assert _decision(await hook(trav, "t", CTX)) == "deny"
    bash = {"tool_name": "Bash", "tool_input": {
        "command": "echo '{}' > /config/plugins/registry.json"}}
    assert _decision(await hook(bash, "t", CTX)) == "deny"


async def test_plugin_guard_blocks_notebook_edit():
    """Sol round-3 B3a: NotebookEdit to /config/plugins is denied (the matcher
    now routes it too)."""
    from hooks import make_agent_home_settings_guard, agent_home_settings_guard_matcher
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "NotebookEdit", "tool_input": {
        "notebook_path": "/config/plugins/store/x/nb.ipynb"}}
    assert _decision(await hook(data, "t", CTX)) == "deny"
    assert "NotebookEdit" in agent_home_settings_guard_matcher().matcher


async def test_plugin_guard_blocks_python_open_write_and_chmod():
    """Sol round-3 B3a: a python `open(...,'w')` write and a chmod of the path
    are denied; a plain store READ is allowed."""
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    for cmd in (
        "python3 -c 'open(\"/config/plugins/registry.json\",\"w\").write(\"x\")'",
        "chmod +w /config/plugins/registry.json",
    ):
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        assert _decision(await hook(data, "t", CTX)) == "deny", cmd
    read = {"tool_name": "Bash", "tool_input": {
        "command": "cat /config/plugins/store/x/y/plugin.json"}}
    assert await hook(read, "t", CTX) == {}         # read allowed


async def test_settings_guard_ignores_non_write_tools():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "Read", "tool_input": {
        "file_path": "/config/agent-home/assistant/.claude/settings.json"}}
    assert await hook(data, "t", CTX) == {}


async def test_settings_guard_matcher_shape_and_denies():
    """I-2 wiring: the injected matcher targets the write tools and its
    callback denies a settings.json edit."""
    from hooks import agent_home_settings_guard_matcher
    m = agent_home_settings_guard_matcher()
    assert m.matcher == "Write|Edit|MultiEdit|NotebookEdit|Bash"
    assert len(m.hooks) == 1
    data = {"tool_name": "Edit", "tool_input": {
        "file_path": "/config/agent-home/assistant/.claude/settings.json"}}
    assert _decision(await m.hooks[0](data, "t", CTX)) == "deny"


# I-2 Bash coverage (codex review v0.69.10, Finding 1): Ellen has Bash, so the
# Write/Edit-only guard was bypassable via `echo > settings.json`.


async def test_settings_guard_blocks_bash_redirect_to_settings():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    for cmd in (
        "echo '{}' > /config/agent-home/assistant/.claude/settings.json",
        "cat x >> /config/agent-home/butler/.claude/settings.json",
        "tee /config/agent-home/assistant/.claude/settings.json < x",
        "sed -i 's/a/b/' /config/agent-home/assistant/.claude/settings.json",
    ):
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        assert _decision(await hook(data, "t", CTX)) == "deny", cmd


async def test_settings_guard_allows_bash_read_of_settings():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "Bash", "tool_input": {
        "command": "cat /config/agent-home/assistant/.claude/settings.json"}}
    assert await hook(data, "t", CTX) == {}


async def test_settings_guard_allows_unrelated_bash():
    from hooks import make_agent_home_settings_guard
    hook = make_agent_home_settings_guard()
    data = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp > out.txt"}}
    assert await hook(data, "t", CTX) == {}
