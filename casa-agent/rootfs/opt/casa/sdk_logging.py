"""SDK message-loop logging primitives (Phase 4b — Bug 3 + Bug 4).

One module owns every per-message log shape and the stderr-callback
factory. All consumers (in_casa-driver._deliver_turn,
agent._attempt_sdk_turn, observer._decide_interjection,
tools.delegate_to_agent + _synthesize_answer) call into here so the
log shape is identical and tested in one place.

Loggers:
- "sdk"            : per-message dispatch (assistant_message, tool_use,
                     tool_result, turn_done, system_init).
- "subprocess_cli" : stderr-callback messages (Bug 4) + claude_code
                     log-line relay (G5 — emitted from
                     drivers/claude_code_driver.py at DEBUG).

Per-record `engagement_id` is passed via ``extra={"engagement_id": short}``
so it flows through ``log_cid.JsonFormatter`` and ``HumanFormatter``
(both already merge non-standard LogRecord attrs — verified Task 1 +
test_log_cid.py::TestExtrasFlatten).
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger("sdk")
_stderr_logger = logging.getLogger("subprocess_cli")


# ---------------------------------------------------------------------------
# Tool-target extraction (§6.2 priority-ordered rules)
# ---------------------------------------------------------------------------


_TARGET_FIELDS = ("file_path", "path", "pattern", "command")
_TARGET_MAX = 80


def extract_tool_target(block: ToolUseBlock) -> str:
    """Render a short label for what a ToolUseBlock targets.

    Priority (first match wins):
      1. block.input["file_path"]
      2. block.input["path"]
      3. block.input["pattern"]
      4. block.input["command"]  (truncated at first newline)
      5. first non-empty string-typed value in block.input
      6. ""

    Result truncated to 80 chars.
    """
    inp = getattr(block, "input", {}) or {}
    if not isinstance(inp, dict):
        return ""
    for field in _TARGET_FIELDS:
        val = inp.get(field)
        if isinstance(val, str) and val:
            if field == "command":
                val = val.split("\n", 1)[0]
            return val[:_TARGET_MAX]
    # Fallback — first string-typed value (skip bool/int/None/list/dict).
    for v in inp.values():
        if isinstance(v, str) and v:
            return v[:_TARGET_MAX]
    return ""


# ---------------------------------------------------------------------------
# Per-message dispatch
# ---------------------------------------------------------------------------


def log_system_init(sdk_msg: SystemMessage) -> None:
    """DEBUG ``system_init model=<m> session_id=<short>``. Subtype-init only."""
    if getattr(sdk_msg, "subtype", None) != "init":
        return
    data = getattr(sdk_msg, "data", {}) or {}
    model = data.get("model", "?")
    sid = data.get("session_id") or ""
    short_sid = sid[:8] if sid else "-"
    logger.debug("system_init model=%s session_id=%s", model, short_sid)


def log_assistant_message(sdk_msg: AssistantMessage, *, idx: int) -> None:
    """INFO ``assistant_message idx=N chars=N tool_uses=N``."""
    chars = 0
    tool_uses = 0
    for block in getattr(sdk_msg, "content", []) or []:
        if isinstance(block, TextBlock):
            chars += len(getattr(block, "text", "") or "")
        elif isinstance(block, ToolUseBlock):
            tool_uses += 1
    logger.info(
        "assistant_message idx=%d chars=%d tool_uses=%d",
        idx, chars, tool_uses,
    )


def log_tool_use(block: ToolUseBlock, *, idx: int) -> None:
    """DEBUG ``tool_use idx=N name=<name> target=<target>``."""
    name = getattr(block, "name", "?")
    target = extract_tool_target(block)
    logger.debug("tool_use idx=%d name=%s target=%s", idx, name, target)


def log_tool_result(
    block: ToolResultBlock, *, idx: int, started_ms: float,
    name: str = "",
) -> None:
    """DEBUG ``tool_result idx=N name=<name> ok=<bool> ms=N``.

    ``ms`` is wall-clock duration since the turn started (started_ms is
    the per-turn anchor captured by the caller).
    """
    is_error = bool(getattr(block, "is_error", False))
    now_ms = time.monotonic() * 1000
    elapsed = int(now_ms - started_ms)
    logger.debug(
        "tool_result idx=%d name=%s ok=%s ms=%d",
        idx, name or "?", not is_error, elapsed,
    )


def log_turn_done(sdk_msg: ResultMessage, *, started_ms: float) -> None:
    """INFO ``turn_done turns=N cost_usd=N.NNNN in_tok=N out_tok=N ms=N``."""
    turns = getattr(sdk_msg, "num_turns", 0) or 0
    cost = float(getattr(sdk_msg, "total_cost_usd", 0.0) or 0.0)
    usage = getattr(sdk_msg, "usage", {}) or {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    now_ms = time.monotonic() * 1000
    elapsed = int(now_ms - started_ms)
    logger.info(
        "turn_done turns=%d cost_usd=%.4f in_tok=%d out_tok=%d ms=%d",
        turns, cost, in_tok, out_tok, elapsed,
    )


# ---------------------------------------------------------------------------
# Stderr-callback factory + ClaudeAgentOptions wrapper (Bug 4)
# ---------------------------------------------------------------------------


def make_stderr_logger(*, engagement_id: str | None) -> Callable[[str], None]:
    """Return a stderr callback for ``ClaudeAgentOptions.stderr``.

    Each invocation emits one INFO record on the ``subprocess_cli``
    logger. engagement_id (if set, truncated to 8 chars) flows onto
    the record as ``extra={"engagement_id": short}`` so the existing
    JsonFormatter and HumanFormatter render it without changes (Task 1
    verification).

    The SDK swallows callback exceptions (subprocess_cli.py) so we
    don't need to defend here.
    """
    short = engagement_id[:8] if engagement_id else None

    def _cb(line: str) -> None:
        line = (line or "").rstrip()
        if not line:
            return
        if short:
            _stderr_logger.info(
                "stderr %s", line, extra={"engagement_id": short},
            )
        else:
            _stderr_logger.info("stderr %s", line)

    return _cb


def with_stderr_callback(
    options: ClaudeAgentOptions, *, engagement_id: str | None,
) -> ClaudeAgentOptions:
    """Return a copy of options with our stderr callback if not already set.

    Caller provides engagement_id where available (in_casa start /
    resume / observer / query_engager) and None otherwise (Ellen DM,
    delegate_to_agent specialist invocation). Caller-provided
    ``stderr=`` callbacks (vanishingly rare in Casa code today) are
    preserved — we never overwrite.

    ``dataclasses.replace`` works because ``ClaudeAgentOptions`` is a
    frozen dataclass per SDK types.py (verified at SDK 0.1.61). The
    same ``replace`` pattern is already used in agent.py to clear
    ``resume``.
    """
    if getattr(options, "stderr", None) is not None:
        return options
    return replace(
        options, stderr=make_stderr_logger(engagement_id=engagement_id),
    )


__all__ = [
    "extract_tool_target",
    "log_system_init",
    "log_assistant_message",
    "log_tool_use",
    "log_tool_result",
    "log_turn_done",
    "make_stderr_logger",
    "with_stderr_callback",
]
