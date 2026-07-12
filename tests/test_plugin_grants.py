"""P-5 (unified plugin arch): plugin MCP-tool grants + required env vars
derived from the RESOLVED artifact path; fail-closed can_use_tool.

Grant namespace per code.claude.com/docs/en/mcp.md ("Plugin MCP tool names"):
mcp__plugin_<plugin>_<server>; server-level (no __<tool> suffix). The
resident/specialist/executor option-builder integration tests live in
tests/test_agent_plugin_binding.py (Task 7)."""
from __future__ import annotations

import json
import logging

import pytest

from plugin_grants import (
    grants_for_resolved,
    grants_for_resolution,
    make_fail_closed_can_use_tool,
    required_env_vars_for_resolved,
    sanitize_segment,
)
from plugin_registry import ResolutionResult, ResolvedPlugin

pytestmark = pytest.mark.unit


def _artifact(tmp_path, name="lesina-invoice", servers=None) -> ResolvedPlugin:
    """A store-artifact dir with an optional .mcp.json at its ROOT, wrapped as
    a ResolvedPlugin (the resolved object grants derive from)."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if servers is not None:
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8")
    return ResolvedPlugin(name=name, artifact_id="0" * 64, path=str(root),
                          version="1.0.0", manifest={})


def test_sanitize_keeps_hyphens_and_underscores():
    assert sanitize_segment("lesina-invoice") == "lesina-invoice"
    assert sanitize_segment("a_b-c9") == "a_b-c9"


def test_sanitize_replaces_other_chars():
    assert sanitize_segment("my plugin.v2") == "my_plugin_v2"


def test_grants_for_resolved_live_pin(tmp_path):
    """Pin the exact live-verified string (guards format drift)."""
    rp = _artifact(tmp_path, "lesina-invoice",
                   {"lesina-invoice": {"command": "node"}})
    assert grants_for_resolved(rp) == ["mcp__plugin_lesina-invoice_lesina-invoice"]


def test_grants_for_resolved_multi_server(tmp_path):
    rp = _artifact(tmp_path, "multi", {"alpha": {}, "beta": {}})
    assert grants_for_resolved(rp) == [
        "mcp__plugin_multi_alpha", "mcp__plugin_multi_beta",
    ]


def test_grants_for_resolved_skill_only_is_empty(tmp_path):
    rp = _artifact(tmp_path, "skills-only", servers=None)  # no .mcp.json
    assert grants_for_resolved(rp) == []


def test_grants_for_resolved_sanitizes_segments(tmp_path):
    rp = _artifact(tmp_path, "weird-name", {"srv.one two": {}})
    assert grants_for_resolved(rp) == ["mcp__plugin_weird-name_srv_one_two"]


def test_grants_for_resolved_corrupt_mcp_json_degrades(tmp_path, caplog):
    rp = _artifact(tmp_path, "bad", servers={"srv": {}})
    (tmp_path / "bad" / ".mcp.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="plugin_grants"):
        assert grants_for_resolved(rp) == []
    assert any("bad" in r.message for r in caplog.records)


def test_grants_for_resolution_unions_and_sorts(tmp_path):
    a = _artifact(tmp_path, "aa", {"zeta": {}})
    b = _artifact(tmp_path, "bb", {"alpha": {}, "beta": {}})
    skill = _artifact(tmp_path, "cc", servers=None)
    res = ResolutionResult(registry_valid=True, plugins=[a, b, skill])
    assert grants_for_resolution(res) == [
        "mcp__plugin_aa_zeta", "mcp__plugin_bb_alpha", "mcp__plugin_bb_beta",
    ]


def test_required_env_vars_extracted(tmp_path):
    rp = _artifact(tmp_path, "needs-secret",
                   {"srv": {"env": {"KEY": "${MY_API_KEY}"}}})
    assert required_env_vars_for_resolved(rp) == ["MY_API_KEY"]


def test_required_env_vars_skill_only_returns_empty(tmp_path):
    """Sol F4: no .mcp.json → [] with no exception."""
    rp = _artifact(tmp_path, "skill", servers=None)
    assert required_env_vars_for_resolved(rp) == []


def test_required_env_vars_malformed_json_degrades(tmp_path):
    rp = _artifact(tmp_path, "corrupt", servers={"srv": {}})
    (tmp_path / "corrupt" / ".mcp.json").write_text("{broken", encoding="utf-8")
    assert required_env_vars_for_resolved(rp) == []


async def test_fail_closed_callback_denies_with_log(caplog):
    from claude_agent_sdk import PermissionResultDeny
    cb = make_fail_closed_can_use_tool("finance")
    with caplog.at_level(logging.WARNING, logger="plugin_grants"):
        result = await cb("mcp__something__tool", {"x": 1}, None)
    assert isinstance(result, PermissionResultDeny)
    assert "mcp__something__tool" in result.message
    assert "finance" in result.message
    assert result.interrupt is False
