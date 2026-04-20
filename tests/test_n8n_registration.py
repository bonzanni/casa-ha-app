"""Tests for casa_core._maybe_register_n8n — Phase 3.4."""

from __future__ import annotations


class TestMaybeRegisterN8n:
    def test_url_unset_does_not_register(self):
        from casa_core import _maybe_register_n8n
        from mcp_registry import McpServerRegistry

        reg = McpServerRegistry()
        result = _maybe_register_n8n(reg, env={})
        assert result is None
        # Registry never saw n8n-workflows.
        resolved = reg.resolve(["n8n-workflows"])
        assert resolved == {}

    def test_url_set_with_api_key_registers_with_bearer_header(self):
        from casa_core import _maybe_register_n8n
        from mcp_registry import McpServerRegistry

        reg = McpServerRegistry()
        env = {"N8N_URL": "http://n8n.local/mcp", "N8N_API_KEY": "sk-abc123"}
        result = _maybe_register_n8n(reg, env=env)
        assert result is not None
        assert result["type"] == "http"
        assert result["url"] == "http://n8n.local/mcp"
        assert result["headers"] == {"Authorization": "Bearer sk-abc123"}
        # Same config is addressable through the registry now.
        resolved = reg.resolve(["n8n-workflows"])
        assert "n8n-workflows" in resolved
        assert resolved["n8n-workflows"]["url"] == "http://n8n.local/mcp"

    def test_url_set_without_api_key_registers_without_headers(self):
        from casa_core import _maybe_register_n8n
        from mcp_registry import McpServerRegistry

        reg = McpServerRegistry()
        env = {"N8N_URL": "http://n8n.local/mcp"}
        result = _maybe_register_n8n(reg, env=env)
        assert result is not None
        assert result["url"] == "http://n8n.local/mcp"
        assert result["headers"] is None

    def test_url_whitespace_only_treated_as_unset(self):
        from casa_core import _maybe_register_n8n
        from mcp_registry import McpServerRegistry

        reg = McpServerRegistry()
        result = _maybe_register_n8n(reg, env={"N8N_URL": "   "})
        assert result is None
        assert reg.resolve(["n8n-workflows"]) == {}
