"""Pure transition tests for Tina's direct-HA voice turn guard."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from voice_turn_guard import VoiceTurnGuard
from error_kinds import (
    ErrorKind,
    VoiceToolLoopError,
    _classify_error,
    _USER_MESSAGES,
)


pytestmark = pytest.mark.unit


def tool_use(tool_id: str, name: str, tool_input: dict) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_id, name=name, input=tool_input)],
        model="claude-haiku-4-5",
    )


def tool_result(
    tool_id: str, *, is_error: bool, text: str = "",
) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(
        tool_use_id=tool_id,
        content=text,
        is_error=is_error,
    )])


def test_one_successful_live_context_is_allowed():
    guard = VoiceTurnGuard.ha_direct()
    guard.observe(tool_use("1", "mcp__homeassistant__GetLiveContext", {}))
    guard.observe(tool_result("1", is_error=False))
    assert guard.live_context_successes == 1


def test_second_live_context_after_success_is_stopped_before_another_round():
    guard = VoiceTurnGuard.ha_direct()
    guard.observe(tool_use("1", "mcp__homeassistant__GetLiveContext", {}))
    guard.observe(tool_result("1", is_error=False))
    with pytest.raises(VoiceToolLoopError, match="live_context_repeat"):
        guard.observe(tool_use(
            "2", "mcp__homeassistant__GetLiveContext", {},
        ))


def test_one_validation_correction_is_allowed_but_second_failure_stops():
    guard = VoiceTurnGuard.ha_direct()
    guard.observe(tool_use(
        "1", "mcp__homeassistant__GetLiveContext", {"domain": "light"},
    ))
    guard.observe(tool_result(
        "1", is_error=True, text="InputValidationError",
    ))
    guard.observe(tool_use("2", "mcp__homeassistant__GetLiveContext", {}))
    with pytest.raises(
        VoiceToolLoopError, match="validation_correction_exhausted",
    ):
        guard.observe(tool_result(
            "2", is_error=True, text="InputValidationError",
        ))


def test_non_validation_tool_error_does_not_consume_correction():
    guard = VoiceTurnGuard.ha_direct()
    guard.observe(tool_use(
        "1", "mcp__homeassistant__HassTurnOff", {"name": "office"},
    ))
    guard.observe(tool_result(
        "1", is_error=True, text="service call failed",
    ))
    assert guard.validation_failures == 0


def test_voice_tool_loop_error_has_typed_classification_and_generic_message():
    error = VoiceToolLoopError("live_context_repeat")
    assert _classify_error(error) is ErrorKind.VOICE_TOOL_LOOP
    assert _USER_MESSAGES[ErrorKind.VOICE_TOOL_LOOP] == (
        "I couldn't resolve that cleanly. Try naming the device again."
    )


def test_butler_runtime_has_tina_voice_tool_loop_line():
    runtime_path = (
        Path(__file__).parents[1]
        / "casa-agent/rootfs/opt/casa/defaults/agents/butler/runtime.yaml"
    )
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert runtime["voice_errors"]["voice_tool_loop"] == (
        "[apologetic] I couldn't resolve that cleanly. "
        "Try naming the device again?"
    )
