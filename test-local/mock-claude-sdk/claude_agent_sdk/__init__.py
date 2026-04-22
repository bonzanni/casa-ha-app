"""Deterministic offline shim for claude_agent_sdk.

Records every call to /data/mock_sdk_calls.jsonl (one JSON object per line).
Responds with canned text. Honours the ``resume`` parameter so that
session_id stays stable across a conversation.

Extra knobs (env vars read lazily each call):
- ``MOCK_SDK_LATENCY_SEC``: sleep before returning (for concurrency tests).
- ``MOCK_SDK_REPLY``: override the reply text (default "ok").
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CALL_LOG = "/data/mock_sdk_calls.jsonl"


def _log(entry: dict[str, Any]) -> None:
    entry = {"ts": time.time(), **entry}
    try:
        with open(CALL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list[Any] = field(default_factory=list)


@dataclass
class ResultMessage:
    session_id: str
    subtype: str = "result"
    # Optional spec 5.2 §5 usage fields. Default empty so historical
    # E2E scenarios (no MOCK_SDK_USAGE_* env) keep working.
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class SystemMessage:
    subtype: str = "init"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookMatcher:
    matcher: str
    hooks: list[Any] = field(default_factory=list)


@dataclass
class ClaudeAgentOptions:
    model: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = ""
    max_turns: int = 0
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    resume: str | None = None
    setting_sources: list[str] = field(default_factory=list)


class ClaudeSDKClient:
    """Async-context-manager mock. Yields a fixed response stream."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self._last_prompt: str = ""
        # Reuse session id on resume so registry round-tripping is observable.
        self._session_id: str = options.resume or f"mock-{uuid.uuid4().hex[:12]}"
        # Public attribute mirroring the real SDK's ClaudeSDKClient.session_id.
        self.session_id: str = self._session_id
        _log({
            "event": "client_init",
            "model": options.model,
            "resume": options.resume,
            "assigned_session_id": self._session_id,
        })

    async def __aenter__(self) -> "ClaudeSDKClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _log({"event": "client_exit", "session_id": self._session_id})

    async def query(self, prompt: str) -> None:
        self._last_prompt = prompt
        _log({
            "event": "query",
            "session_id": self._session_id,
            "prompt_len": len(prompt),
        })

    async def receive_response(self):
        latency = float(os.environ.get("MOCK_SDK_LATENCY_SEC", "0") or "0")
        if latency > 0:
            await asyncio.sleep(latency)

        # Phase 3.1: finer-grained knob for delegate_to_specialist degradation
        # tests — milliseconds, stacks with MOCK_SDK_LATENCY_SEC.
        delay_ms = float(os.environ.get("MOCK_SDK_DELAY_MS", "0") or "0")
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

        reply = os.environ.get("MOCK_SDK_REPLY", "ok")

        yield SystemMessage(subtype="init", data={"session_id": self._session_id})
        yield AssistantMessage(content=[TextBlock(text=reply)])
        yield ResultMessage(
            session_id=self._session_id,
            usage={
                "input_tokens": int(os.environ.get("MOCK_SDK_USAGE_INPUT", "0") or "0"),
                "output_tokens": int(os.environ.get("MOCK_SDK_USAGE_OUTPUT", "0") or "0"),
                "cache_read_input_tokens": int(
                    os.environ.get("MOCK_SDK_USAGE_CACHE_READ", "0") or "0"
                ),
                "cache_creation_input_tokens": int(
                    os.environ.get("MOCK_SDK_USAGE_CACHE_WRITE", "0") or "0"
                ),
            },
        )


def tool(name: str, description: str, params: dict[str, Any]) -> Any:
    """Mock decorator for @tool. Just returns the wrapped function unchanged."""
    def decorator(func):
        return func
    return decorator


def create_sdk_mcp_server(name: str = "", tools: list[Any] | None = None) -> dict[str, Any]:
    """Mock MCP server factory. Returns a minimal config dict."""
    return {
        "type": "stdio",
        "command": "echo",
        "args": ["mock"],
    }


class ProcessError(Exception):
    """Mock of claude_agent_sdk.ProcessError. Raised when the CLI process fails.

    Matches the real SDK's 3-arg signature so tests that construct it with
    ``ProcessError("msg", exit_code=1)`` or ``exit_code=1, stderr="..."``
    work unchanged.
    """

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        if exit_code is not None:
            message = f"{message} (exit code: {exit_code})"
        if stderr:
            message = f"{message}\nError output: {stderr}"
        super().__init__(message)


__all__ = [
    "AssistantMessage",
    "ClaudeAgentOptions",
    "ClaudeSDKClient",
    "HookMatcher",
    "ProcessError",
    "ResultMessage",
    "SystemMessage",
    "TextBlock",
    "tool",
    "create_sdk_mcp_server",
]
