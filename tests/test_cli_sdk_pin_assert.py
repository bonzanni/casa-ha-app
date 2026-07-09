"""Guard: the global claude CLI install must be version-pinned and the
claude-agent-sdk pin must be exact + match the intended version (spec §6).
Pure-unit static parse — no docker, no network."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "casa-agent" / "Dockerfile"
REQUIREMENTS = REPO / "casa-agent" / "requirements.txt"

SDK_VERSION = "0.2.114"
CLI_VERSION = "2.1.150"


def test_global_claude_cli_is_pinned() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(
        r"@anthropic-ai/claude-code(?:\s|\\|$)(?!@)", text
    ), "global claude CLI install is unpinned"
    assert f"@anthropic-ai/claude-code@{CLI_VERSION}" in text, (
        f"Dockerfile must pin @anthropic-ai/claude-code@{CLI_VERSION}"
    )


def test_sdk_pin_is_exact_and_current() -> None:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    sdk_lines = [line for line in lines if line.strip().startswith("claude-agent-sdk")]
    assert sdk_lines, "claude-agent-sdk not pinned in requirements.txt"
    assert sdk_lines[0].strip() == f"claude-agent-sdk=={SDK_VERSION}", (
        f"expected claude-agent-sdk=={SDK_VERSION}, got {sdk_lines[0].strip()!r}"
    )


@pytest.fixture(scope="session")
def _pin_image_tag() -> str:
    return "casa-agent:local-clipin"


@pytest.fixture(scope="session")
def _build_pin_image(_pin_image_tag: str) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    subprocess.run(
        ["docker", "build",
         "--build-arg", "BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm",
         "-t", _pin_image_tag, "-f", "casa-agent/Dockerfile", "casa-agent/"],
        check=True,
    )


@pytest.mark.docker
def test_image_claude_cli_version(_pin_image_tag: str, _build_pin_image: None) -> None:
    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", _pin_image_tag,
         "-c", "claude --version"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"claude --version failed: {r.stderr}"
    assert CLI_VERSION in r.stdout, (
        f"image claude version {r.stdout.strip()!r} != pinned {CLI_VERSION}"
    )
