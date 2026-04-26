"""Verify CASA_HA_MCP_URL env override is honored when present."""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


def test_default_ha_mcp_url(monkeypatch):
    """Without env override, the canonical supervisor URL is used."""
    from mcp_registry import McpServerRegistry

    monkeypatch.delenv("CASA_HA_MCP_URL", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    reg = McpServerRegistry()
    ha_url = os.environ.get("CASA_HA_MCP_URL", "http://supervisor/core/api/mcp")
    reg.register_http(
        name="homeassistant",
        url=ha_url,
        headers={"Authorization": "Bearer test-token"},
    )
    resolved = reg.resolve(["homeassistant"])
    assert resolved["homeassistant"]["url"] == "http://supervisor/core/api/mcp"


def test_ha_mcp_url_override(monkeypatch):
    """With CASA_HA_MCP_URL set, that URL is used instead."""
    from mcp_registry import McpServerRegistry

    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setenv("CASA_HA_MCP_URL", "http://mock-ha:8200/")

    reg = McpServerRegistry()
    ha_url = os.environ.get("CASA_HA_MCP_URL", "http://supervisor/core/api/mcp")
    reg.register_http(
        name="homeassistant",
        url=ha_url,
        headers={"Authorization": "Bearer test-token"},
    )
    resolved = reg.resolve(["homeassistant"])
    assert resolved["homeassistant"]["url"] == "http://mock-ha:8200/"
