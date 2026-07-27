"""Bound repeated Home Assistant tool loops during direct voice turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ToolResultBlock, ToolUseBlock

from error_kinds import VoiceToolLoopError


VALIDATION_MARKERS = (
    "inputvalidationerror",
    "invalid tool input",
    "input validation failed",
    "schema validation",
)


@dataclass(frozen=True)
class _ToolUse:
    is_live_context: bool


@dataclass
class VoiceTurnGuard:
    """Pure observer for one ``ha_direct`` voice SDK attempt."""

    live_context_successes: int = 0
    validation_failures: int = 0
    _tool_uses: dict[str, _ToolUse] = field(default_factory=dict)

    @classmethod
    def ha_direct(cls) -> "VoiceTurnGuard":
        return cls()

    def observe(self, sdk_msg: Any) -> None:
        """Observe tool-use/result blocks without changing the SDK message."""
        for block in getattr(sdk_msg, "content", []) or []:
            if isinstance(block, ToolUseBlock):
                self._observe_tool_use(block)
            elif isinstance(block, ToolResultBlock):
                self._observe_tool_result(block)

    def _observe_tool_use(self, block: ToolUseBlock) -> None:
        is_live_context = str(getattr(block, "name", "")).endswith(
            "GetLiveContext",
        )
        if is_live_context and self.live_context_successes >= 1:
            raise VoiceToolLoopError("live_context_repeat")
        self._tool_uses[str(getattr(block, "id", ""))] = _ToolUse(
            is_live_context=is_live_context,
        )

    def _observe_tool_result(self, block: ToolResultBlock) -> None:
        tool_use = self._tool_uses.pop(
            str(getattr(block, "tool_use_id", "")), None,
        )
        if tool_use is None:
            return

        is_error = bool(getattr(block, "is_error", False))
        if not is_error:
            if tool_use.is_live_context:
                self.live_context_successes += 1
            return

        text = _normalized_result_text(getattr(block, "content", ""))
        if any(marker in text for marker in VALIDATION_MARKERS):
            self.validation_failures += 1
            if self.validation_failures > 1:
                raise VoiceToolLoopError("validation_correction_exhausted")


def _normalized_result_text(content: Any) -> str:
    """Return case- and whitespace-normalized text for marker matching."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    else:
        text = str(content or "")
    return " ".join(text.lower().split())
