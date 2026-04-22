"""Tests for the config_git_commit MCP tool."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


class TestConfigGitCommitTool:
    async def test_happy_path(self):
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value="abc123def"):
            result = await config_git_commit.handler({"message": "test"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == "abc123def"
        assert payload["message"] == "test"

    async def test_noop_returns_empty_sha(self):
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value=""):
            result = await config_git_commit.handler({"message": "noop"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == ""

    async def test_raises_bubbles_as_error_kind(self):
        from tools import config_git_commit
        with patch("config_git.commit_config", side_effect=RuntimeError("git broke")):
            result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert "git broke" in payload["message"]
