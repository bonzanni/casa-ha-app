"""Unit tests for sdk_logging — per-message dispatch + stderr callback (Phase 4b)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest


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
    def test_emits_debug_with_name_and_elapsed_ms_without_arguments(self, caplog):
        from sdk_logging import log_tool_use
        secret = "SECRET_TOOL_ARGUMENT"
        block = _mk_tool_use(
            "mcp__homeassistant__HassTurnOff",
            {"name": secret},
        )

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            log_tool_use(
                block,
                idx=1,
                started_ms=1000.0,
                monotonic=lambda: 1.125,
            )

        recs = [r for r in caplog.records if r.name == "sdk"]
        assert len(recs) == 1
        assert recs[0].levelno == logging.DEBUG
        msg = recs[0].getMessage()
        assert "tool_use" in msg
        assert msg == (
            "tool_use idx=1 name=mcp__homeassistant__HassTurnOff ms=125"
        )
        assert secret not in caplog.text


class TestLogToolResult:
    def test_emits_debug_with_ok_and_ms(self, caplog):
        from sdk_logging import log_tool_result
        block = _mk_tool_result(is_error=False, content="42 lines read")

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            log_tool_result(
                block,
                idx=2,
                started_ms=1000.0,
                name="Read",
                monotonic=lambda: 1.125,
            )

        recs = [r for r in caplog.records if r.name == "sdk"]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "tool_result" in msg
        assert "idx=2" in msg
        assert "name=Read" in msg
        assert "ok=True" in msg
        assert "ms=125" in msg


class TestLogTurnDone:
    def test_emits_info_with_cost_and_tokens(self, caplog):
        from sdk_logging import log_turn_done
        sdk_msg = MagicMock()
        sdk_msg.num_turns = 3
        sdk_msg.total_cost_usd = 0.0042
        sdk_msg.usage = {"input_tokens": 1234, "output_tokens": 567}
        with caplog.at_level(logging.INFO, logger="sdk"):
            log_turn_done(
                sdk_msg,
                started_ms=1000.0,
                monotonic=lambda: 1.250,
            )

        recs = [r for r in caplog.records if r.name == "sdk"]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "turn_done" in msg
        assert "turns=3" in msg
        assert "cost_usd=0.0042" in msg
        assert "in_tok=1234" in msg
        assert "out_tok=567" in msg
        # E2: cache fields default to 0 when the usage dict omits them.
        assert "cache_read=0" in msg
        assert "cache_write=0" in msg
        assert "ms=250" in msg

    def test_emits_cache_token_fields(self, caplog):
        """E2: cache_read/cache_write come from the Anthropic usage keys so a
        cached prompt's low in_tok with real cost is explainable."""
        from sdk_logging import log_turn_done
        sdk_msg = MagicMock()
        sdk_msg.num_turns = 1
        sdk_msg.total_cost_usd = 0.0755
        sdk_msg.usage = {
            "input_tokens": 3, "output_tokens": 12,
            "cache_read_input_tokens": 18500,
            "cache_creation_input_tokens": 1200,
        }
        with caplog.at_level(logging.INFO, logger="sdk"):
            log_turn_done(
                sdk_msg,
                started_ms=1000.0,
                monotonic=lambda: 1.100,
            )

        msg = [r for r in caplog.records if r.name == "sdk"][0].getMessage()
        assert "in_tok=3" in msg
        assert "cache_read=18500" in msg
        assert "cache_write=1200" in msg


class TestVoiceToolLoopStop:
    @pytest.mark.asyncio
    async def test_logs_reason_and_counts_without_tool_payload(self, caplog):
        from agent import Agent
        from claude_agent_sdk import (
            AssistantMessage,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )
        from error_kinds import VoiceToolLoopError
        from voice_turn_guard import VoiceTurnGuard

        secret = "SECRET_VALIDATION_RESULT"
        agent = Agent.__new__(Agent)
        on_message, _state = agent._make_on_message(
            None,
            VoiceTurnGuard.ha_direct(),
        )

        async def tool_round(tool_id: str) -> None:
            await on_message(AssistantMessage(
                content=[ToolUseBlock(
                    id=tool_id,
                    name="mcp__homeassistant__GetLiveContext",
                    input={"domain": "light"},
                )],
                model="claude-haiku-4-5",
            ))
            await on_message(UserMessage(content=[ToolResultBlock(
                tool_use_id=tool_id,
                content=f"InputValidationError {secret}",
                is_error=True,
            )]))

        await tool_round("tool-1")
        with caplog.at_level(logging.INFO, logger="agent"):
            with pytest.raises(
                VoiceToolLoopError,
                match="validation_correction_exhausted",
            ):
                await tool_round("tool-2")

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "agent" and "voice_tool_loop_stop" in record.getMessage()
        ]
        assert messages == [
            "voice_tool_loop_stop "
            "reason=validation_correction_exhausted "
            "live_context_successes=0 validation_failures=2"
        ]
        assert secret not in caplog.text


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


class TestStructuredVoiceSdkLogGuard:
    @pytest.mark.parametrize("logger_name", [
        "claude_agent_sdk._internal.query",
        "claude_agent_sdk._internal.transport.subprocess_cli",
        "claude_agent_sdk._internal._task_compat",
    ])
    def test_suppresses_sdk_payload_logs_only_inside_guard(
        self, caplog, logger_name,
    ):
        from sdk_logging import suppress_structured_voice_sdk_payload_logs

        private_canary = "PRIVATE-MALFORMED-SDK-FRAME-CANARY"
        logger = logging.getLogger(logger_name)
        with caplog.at_level(logging.DEBUG):
            logger.error("visible before guard")
            with suppress_structured_voice_sdk_payload_logs():
                logger.error("malformed frame: %s", private_canary)
            logger.error("visible after guard")

        messages = [
            record.getMessage() for record in caplog.records
            if record.name == logger_name
        ]
        assert messages == ["visible before guard", "visible after guard"]
        assert private_canary not in caplog.text

    @pytest.mark.asyncio
    async def test_detached_guarded_task_stays_filtered_after_parent_exits(
        self, caplog,
    ):
        from sdk_logging import suppress_structured_voice_sdk_payload_logs

        private_canary = "PRIVATE-DETACHED-SDK-CANARY-b2a7"
        release = asyncio.Event()

        async def _late_sdk_log():
            await release.wait()
            logging.getLogger("claude_agent_sdk._internal.query").error(
                "late guarded payload: %s", private_canary,
            )

        with caplog.at_level(logging.DEBUG):
            with suppress_structured_voice_sdk_payload_logs():
                detached = asyncio.create_task(_late_sdk_log())
            release.set()
            await detached

        assert private_canary not in caplog.text

    @pytest.mark.asyncio
    async def test_concurrent_non_voice_context_remains_visible(self, caplog):
        from sdk_logging import suppress_structured_voice_sdk_payload_logs

        guarded_canary = "PRIVATE-CONCURRENT-GUARDED-CANARY-31fa"
        visible_message = "concurrent non-voice sdk diagnostic"
        ready = asyncio.Event()
        release = asyncio.Event()
        logger = logging.getLogger("claude_agent_sdk._internal.query")

        async def _guarded_job():
            with suppress_structured_voice_sdk_payload_logs():
                ready.set()
                await release.wait()
                logger.error("guarded: %s", guarded_canary)

        async def _non_voice_job():
            await ready.wait()
            logger.error(visible_message)
            release.set()

        with caplog.at_level(logging.DEBUG):
            await asyncio.gather(_guarded_job(), _non_voice_job())

        assert guarded_canary not in caplog.text
        assert visible_message in caplog.text

    @pytest.mark.asyncio
    async def test_cancelled_guard_restores_parent_context(self, caplog):
        from sdk_logging import suppress_structured_voice_sdk_payload_logs

        logger = logging.getLogger("claude_agent_sdk._internal.query")
        visible_message = "sdk diagnostic after guarded cancellation"

        async def _cancel_then_log_in_same_context():
            try:
                with suppress_structured_voice_sdk_payload_logs():
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            logger.error(visible_message)

        with caplog.at_level(logging.DEBUG):
            await _cancel_then_log_in_same_context()

        assert visible_message in caplog.text


class TestSdkTaskNoiseFilter:
    """P-4: the SDK spawns control-request handlers as DETACHED tasks; at
    engagement teardown their transport.write raises CLIConnectionError and
    nothing retrieves it -> asyncio GC logs "Task exception was never
    retrieved" at ERROR on every successful engagement close."""

    @staticmethod
    def _ctx(exc):
        return {
            "message": "Task exception was never retrieved",
            "exception": exc,
            "task": object(),
        }

    def test_suppresses_unretrieved_cli_connection_error(self, caplog):
        import asyncio
        from claude_agent_sdk import CLIConnectionError
        from sdk_logging import install_sdk_task_noise_filter

        loop = asyncio.new_event_loop()
        try:
            install_sdk_task_noise_filter(loop)
            handler = loop.get_exception_handler()
            assert handler is not None
            with caplog.at_level(logging.DEBUG):
                handler(loop, self._ctx(
                    CLIConnectionError("ProcessTransport is not ready for writing")))
        finally:
            loop.close()
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
        sdk_records = [r for r in caplog.records if r.name == "sdk"]
        assert sdk_records and sdk_records[-1].levelno == logging.DEBUG
        assert "ProcessTransport is not ready for writing" in sdk_records[-1].getMessage()

    def test_delegates_other_exceptions_to_default(self, caplog):
        import asyncio
        from sdk_logging import install_sdk_task_noise_filter

        loop = asyncio.new_event_loop()
        try:
            install_sdk_task_noise_filter(loop)
            with caplog.at_level(logging.DEBUG):
                loop.get_exception_handler()(loop, self._ctx(ValueError("real bug")))
        finally:
            loop.close()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "non-SDK task exceptions must still reach the default handler"

    def test_cli_not_found_subclass_stays_loud(self, caplog):
        import asyncio
        from claude_agent_sdk import CLINotFoundError
        from sdk_logging import install_sdk_task_noise_filter

        loop = asyncio.new_event_loop()
        try:
            install_sdk_task_noise_filter(loop)
            with caplog.at_level(logging.DEBUG):
                loop.get_exception_handler()(
                    loop, self._ctx(CLINotFoundError("claude binary missing")))
        finally:
            loop.close()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "CLINotFoundError (subclass) is a real fault, never noise"

    def test_context_without_exception_delegates(self, caplog):
        import asyncio
        from sdk_logging import install_sdk_task_noise_filter

        loop = asyncio.new_event_loop()
        try:
            install_sdk_task_noise_filter(loop)
            with caplog.at_level(logging.DEBUG):
                loop.get_exception_handler()(
                    loop, {"message": "Unclosed resource", "source": object()})
        finally:
            loop.close()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "exception-less contexts must still reach the default handler"

    def test_detached_task_death_is_suppressed_end_to_end(self, caplog):
        import asyncio
        import gc
        from claude_agent_sdk import CLIConnectionError
        from sdk_logging import install_sdk_task_noise_filter

        async def scenario():
            install_sdk_task_noise_filter(asyncio.get_running_loop())

            async def boom():
                raise CLIConnectionError("ProcessTransport is not ready for writing")

            task = asyncio.get_running_loop().create_task(boom())
            await asyncio.sleep(0)  # task completes; exception stored, never retrieved
            del task
            gc.collect()  # Task.__del__ -> loop.call_exception_handler

        with caplog.at_level(logging.DEBUG):
            asyncio.run(scenario())
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
