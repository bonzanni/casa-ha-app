"""Guard: the global claude CLI install must be version-pinned and the
claude-agent-sdk pin must be exact + match the intended version (spec §6).
Pure-unit static parse — no docker, no network."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "casa-agent" / "Dockerfile"
REQUIREMENTS = REPO / "casa-agent" / "requirements.txt"

SDK_VERSION = "0.2.87"
CLI_VERSION = "2.1.150"


def test_global_claude_cli_is_pinned() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "npm install -g @anthropic-ai/claude-code\n" not in text
    assert not re.search(
        r"@anthropic-ai/claude-code(?:\s|\\|$)(?!@)", text
    ), "global claude CLI install is unpinned"
    assert f"@anthropic-ai/claude-code@{CLI_VERSION}" in text, (
        f"Dockerfile must pin @anthropic-ai/claude-code@{CLI_VERSION}"
    )


def test_sdk_pin_is_exact_and_current() -> None:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    sdk_lines = [l for l in lines if l.strip().startswith("claude-agent-sdk")]
    assert sdk_lines, "claude-agent-sdk not pinned in requirements.txt"
    assert sdk_lines[0].strip() == f"claude-agent-sdk=={SDK_VERSION}", (
        f"expected claude-agent-sdk=={SDK_VERSION}, got {sdk_lines[0].strip()!r}"
    )
