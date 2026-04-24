"""Baseline runtime tools (§4.5) must all resolve inside the running addon
container. Run under the `docker` marker — skipped if no docker in env."""
from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.docker]

BASELINE_TOOLS = [
    "bash", "sh", "curl", "jq", "yq", "git",
    "python3", "pip3", "node", "npm", "gh", "op", "claude",
    "ca-certificates", "ssh", "openssl", "tar", "gzip", "unzip",
]


@pytest.fixture(scope="session")
def image_tag() -> str:
    return "casa-agent:local-baseline"


@pytest.fixture(scope="session", autouse=True)
def _build_image(image_tag: str) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    subprocess.run(
        ["docker", "build",
         "--build-arg", "BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm",
         "-t", image_tag, "-f", "casa-agent/Dockerfile", "casa-agent/"],
        check=True,
    )


@pytest.mark.parametrize("tool", BASELINE_TOOLS)
def test_tool_on_path(tool: str, image_tag: str) -> None:
    # Special-case: ca-certificates is a package, test for its installed file.
    if tool == "ca-certificates":
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image_tag,
             "-c", "test -f /etc/ssl/certs/ca-certificates.crt"],
        )
    else:
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image_tag,
             "-c", f"command -v {tool}"],
        )
    assert r.returncode == 0, f"baseline runtime regression: {tool} missing"
