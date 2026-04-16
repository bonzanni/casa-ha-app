"""MCP (Model Context Protocol) server registry."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class McpServerRegistry:
    """Registry of named MCP server configurations."""

    def __init__(self) -> None:
        self._servers: dict[str, dict[str, Any]] = {}

    def register_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Register an HTTP-based MCP server."""
        self._servers[name] = {
            "type": "http",
            "url": url,
            "headers": headers,
        }

    def register_sdk(self, name: str, server_config: dict[str, Any]) -> None:
        """Register an MCP server from a raw config dict."""
        self._servers[name] = server_config

    def resolve(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """Return configs for all *names* that are registered.

        Logs a warning for any name that is not found.
        """
        result: dict[str, dict[str, Any]] = {}
        for name in names:
            if name in self._servers:
                result[name] = self._servers[name]
            else:
                logger.warning("MCP server '%s' is not registered", name)
        return result
