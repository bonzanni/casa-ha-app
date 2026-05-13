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
