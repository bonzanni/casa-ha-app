"""Tests for the config_git_commit MCP tool."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def configurator_origin():
    """Bug 7 fix: tool refuses unless caller role == 'configurator'."""
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


class TestConfigGitCommitTool:
    async def test_happy_path(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value="abc123def"):
            result = await config_git_commit.handler({"message": "test"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == "abc123def"
        assert payload["message"] == "test"

    async def test_noop_returns_empty_sha(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config", return_value=""):
            result = await config_git_commit.handler({"message": "noop"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == ""

    async def test_raises_bubbles_as_error_kind(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config", side_effect=RuntimeError("git broke")):
            result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert "git broke" in payload["message"]


class TestConfigGitCommitRoleGuard:
    """Bug 7 (v0.14.6): config_git_commit must refuse non-configurator
    callers even if their runtime.yaml::tools.allowed lists the tool."""

    async def test_no_origin_refused(self):
        from tools import config_git_commit
        result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_assistant_role_refused(self):
        import agent as agent_mod
        from tools import config_git_commit
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            result = await config_git_commit.handler({"message": "x"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_plugin_developer_role_refused(self):
        """Plugin-developer is an executor but not authorised for these tools."""
        import agent as agent_mod
        from tools import config_git_commit
        tok = agent_mod.origin_var.set({"role": "plugin-developer"})
        try:
            result = await config_git_commit.handler({"message": "x"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"
