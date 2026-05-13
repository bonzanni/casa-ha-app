"""Tests for cc_tool_pattern — CC CLI tools.allowed pattern matcher."""

from __future__ import annotations

import pytest


class TestBareToolName:
    def test_bare_matches_same_tool(self):
        from cc_tool_pattern import matches
        assert matches("Read", "Read", {}) is True

    def test_bare_ignores_input(self):
        from cc_tool_pattern import matches
        assert matches("Read", "Read", {"file_path": "anything"}) is True

    def test_bare_does_not_match_other_tool(self):
        from cc_tool_pattern import matches
        assert matches("Read", "Write", {}) is False

    def test_mcp_bare_match(self):
        from cc_tool_pattern import matches
        full = "mcp__casa-framework__memory_read"
        assert matches(full, full, {"key": "v"}) is True


class TestBashSpec:
    def test_npm_prefix_matches(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(npm*)", "Bash", {"command": "npm install"},
        ) is True

    def test_npm_mismatch(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(npm*)", "Bash", {"command": "git push"},
        ) is False

    def test_exact_match(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(git status)", "Bash", {"command": "git status"},
        ) is True

    def test_empty_command(self):
        from cc_tool_pattern import matches
        assert matches("Bash(npm*)", "Bash", {}) is False

    def test_glob_question_mark(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(curl http?://*)", "Bash",
            {"command": "curl https://example.com"},
        ) is True

    def test_pipeline_string_match(self):
        """Documented limitation: matches the raw command string the same
        way CC does (no argv split)."""
        from cc_tool_pattern import matches
        assert matches(
            "Bash(npm*)", "Bash",
            {"command": "npm install && echo ok"},
        ) is True
