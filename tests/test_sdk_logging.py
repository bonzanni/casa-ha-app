"""Unit tests for sdk_logging — per-message dispatch + stderr callback (Phase 4b)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock


def _mk_tool_use(name: str, input_: dict):
    """Build a synthetic ToolUseBlock-shaped object."""
    block = MagicMock()
    block.name = name
    block.input = dict(input_)
    return block


def _mk_tool_result(*, is_error: bool = False, content="ok"):
    block = MagicMock()
    block.is_error = is_error
    block.content = content
    return block


class TestExtractToolTarget:
    """§6.2 priority-ordered target extraction."""

    def test_file_path_wins(self):
        from sdk_logging import extract_tool_target
        block = _mk_tool_use("Edit", {"file_path": "/tmp/foo.py", "old_string": "x"})
        assert extract_tool_target(block) == "/tmp/foo.py"

    def test_path_when_no_file_path(self):
        from sdk_logging import extract_tool_target
        block = _mk_tool_use("Glob", {"path": "/srv", "pattern": "*.py"})
        assert extract_tool_target(block) == "/srv"

    def test_pattern_when_no_file_path_no_path(self):
        from sdk_logging import extract_tool_target
        block = _mk_tool_use("Grep", {"pattern": "TODO"})
        assert extract_tool_target(block) == "TODO"

    def test_command_truncated_at_first_newline(self):
        from sdk_logging import extract_tool_target
        block = _mk_tool_use("Bash", {"command": "ls\n# comment"})
        assert extract_tool_target(block) == "ls"

    def test_first_string_value_fallback(self):
        from sdk_logging import extract_tool_target
        block = _mk_tool_use("Custom", {"size": 42, "label": "hello", "ok": True})
        assert extract_tool_target(block) == "hello"

    def test_empty_when_no_strings(self):
        from sdk_logging import extract_tool_target
        block = _mk_tool_use("Custom", {"size": 42, "ok": True})
        assert extract_tool_target(block) == ""

    def test_truncated_at_80_chars(self):
        from sdk_logging import extract_tool_target
        path = "/a" * 100  # 200 chars
        block = _mk_tool_use("Read", {"file_path": path})
        out = extract_tool_target(block)
        assert len(out) == 80
        assert out == path[:80]


class TestLogAssistantMessage:
    def test_emits_info_with_chars_and_tool_uses(self, caplog):
        from sdk_logging import log_assistant_message
        from claude_agent_sdk import TextBlock, ToolUseBlock

        text_block = MagicMock()
        text_block.text = "Reading the doctrine."
        text_block.__class__ = TextBlock
        tool_block = _mk_tool_use("Read", {"file_path": "/x"})
        tool_block.__class__ = ToolUseBlock

        sdk_msg = MagicMock()
        sdk_msg.content = [text_block, tool_block]

        with caplog.at_level(logging.INFO, logger="sdk"):
            log_assistant_message(sdk_msg, idx=3)

        records = [r for r in caplog.records if r.name == "sdk"]
        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.INFO
        msg = rec.getMessage()
        assert "assistant_message" in msg
        assert "idx=3" in msg
        # chars=21 = len("Reading the doctrine.")
        assert "chars=21" in msg
        assert "tool_uses=1" in msg


class TestLogToolUse:
    def test_emits_debug_with_name_and_target(self, caplog):
        from sdk_logging import log_tool_use
        block = _mk_tool_use("Edit", {"file_path": "/x.py", "old_string": "a"})

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            log_tool_use(block, idx=2)

        recs = [r for r in caplog.records if r.name == "sdk"]
        assert len(recs) == 1
        assert recs[0].levelno == logging.DEBUG
        msg = recs[0].getMessage()
        assert "tool_use" in msg
        assert "idx=2" in msg
        assert "name=Edit" in msg
        assert "target=/x.py" in msg


class TestLogToolResult:
    def test_emits_debug_with_ok_and_ms(self, caplog):
        import re
        import time
        from sdk_logging import log_tool_result
        started_ms = (time.monotonic() * 1000) - 100
        block = _mk_tool_result(is_error=False, content="42 lines read")

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            log_tool_result(block, idx=2, started_ms=started_ms, name="Read")

        recs = [r for r in caplog.records if r.name == "sdk"]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "tool_result" in msg
        assert "idx=2" in msg
        assert "name=Read" in msg
        assert "ok=True" in msg
        m = re.search(r"ms=(\d+)", msg)
        assert m, msg
        assert int(m.group(1)) >= 99  # allow 1 ms slop


class TestLogTurnDone:
    def test_emits_info_with_cost_and_tokens(self, caplog):
        import time
        from sdk_logging import log_turn_done
        sdk_msg = MagicMock()
        sdk_msg.num_turns = 3
        sdk_msg.total_cost_usd = 0.0042
        sdk_msg.usage = {"input_tokens": 1234, "output_tokens": 567}
        started_ms = (time.monotonic() * 1000) - 250

        with caplog.at_level(logging.INFO, logger="sdk"):
            log_turn_done(sdk_msg, started_ms=started_ms)

        recs = [r for r in caplog.records if r.name == "sdk"]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "turn_done" in msg
        assert "turns=3" in msg
        assert "cost_usd=0.0042" in msg
        assert "in_tok=1234" in msg
        assert "out_tok=567" in msg


class TestLogSystemInit:
    def test_emits_debug_with_model_and_session_id(self, caplog):
        from sdk_logging import log_system_init
        sdk_msg = MagicMock()
        sdk_msg.subtype = "init"
        sdk_msg.data = {
            "model": "claude-sonnet-4-6",
            "session_id": "abc12345-def6-7890-1234-567890abcdef",
        }

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            log_system_init(sdk_msg)

        recs = [r for r in caplog.records if r.name == "sdk"]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "system_init" in msg
        assert "model=claude-sonnet-4-6" in msg
        assert "session_id=abc12345" in msg


class TestMakeStderrLogger:
    def test_emits_info_with_engagement_id_extra(self, caplog):
        from sdk_logging import make_stderr_logger
        cb = make_stderr_logger(engagement_id="abc12345")

        with caplog.at_level(logging.INFO, logger="subprocess_cli"):
            cb("Some stderr line\n")

        recs = [r for r in caplog.records if r.name == "subprocess_cli"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec.levelno == logging.INFO
        assert "stderr Some stderr line" in rec.getMessage()
        assert getattr(rec, "engagement_id", None) == "abc12345"

    def test_no_engagement_id_omits_extra(self, caplog):
        from sdk_logging import make_stderr_logger
        cb = make_stderr_logger(engagement_id=None)

        with caplog.at_level(logging.INFO, logger="subprocess_cli"):
            cb("Some stderr line")

        recs = [r for r in caplog.records if r.name == "subprocess_cli"]
        assert len(recs) == 1
        # No engagement_id attr present (extra was empty)
        assert (
            not hasattr(recs[0], "engagement_id")
            or getattr(recs[0], "engagement_id", None) is None
        )

    def test_strips_trailing_newline(self, caplog):
        from sdk_logging import make_stderr_logger
        cb = make_stderr_logger(engagement_id="x12345678")
        with caplog.at_level(logging.INFO, logger="subprocess_cli"):
            cb("with newline\n")
        msg = caplog.records[-1].getMessage()
        assert msg.endswith("with newline"), msg


class TestWithStderrCallback:
    def test_injects_when_unset(self):
        from claude_agent_sdk import ClaudeAgentOptions
        from sdk_logging import with_stderr_callback
        opts = ClaudeAgentOptions(model="sonnet")
        # Pre-condition: stderr default is None per SDK types.py
        assert opts.stderr is None
        out = with_stderr_callback(opts, engagement_id="x12345678")
        assert callable(out.stderr)
        # Original options untouched (dataclasses.replace returns new instance)
        assert opts.stderr is None

    def test_preserves_user_provided_stderr(self):
        from claude_agent_sdk import ClaudeAgentOptions
        from sdk_logging import with_stderr_callback
        sentinel = lambda line: None  # noqa: E731
        opts = ClaudeAgentOptions(model="sonnet", stderr=sentinel)
        out = with_stderr_callback(opts, engagement_id="x12345678")
        # We do NOT clobber a caller-provided callback.
        assert out.stderr is sentinel
