"""MCP (Model Context Protocol) server registry."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

SdkFactory = Callable[[str, frozenset[str]], dict[str, Any]]


class McpServerRegistry:
    """Registry of named MCP server configurations."""

    def __init__(self) -> None:
        self._servers: dict[str, dict[str, Any]] = {}
        self._role_servers: dict[tuple[str, str], dict[str, Any]] = {}
        self._sdk_factories: dict[str, SdkFactory] = {}

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

    def register_sdk_factory(self, name: str, factory: SdkFactory) -> None:
        """Register a role-aware SDK server configuration factory."""
        self._sdk_factories[name] = factory

    def register_role_sdk(
        self,
        name: str,
        role: str,
        server_config: dict[str, Any],
    ) -> None:
        """Register an SDK server override for one role."""
        self._role_servers[(name, role)] = server_config

    def unregister_role_sdk(self, name: str, role: str) -> None:
        """Remove a role-specific SDK server override if present."""
        self._role_servers.pop((name, role), None)

    def resolve(
        self,
        names: list[str],
        *,
        role: str = "",
        allowed_tools: Iterable[str] = (),
    ) -> dict[str, dict[str, Any]]:
        """Return configs for all *names* that are registered.

        Logs a warning for any name that is not found.
        """
        grants = frozenset(allowed_tools)
        result: dict[str, dict[str, Any]] = {}
        for name in names:
            override = self._role_servers.get((name, role))
            if override is not None:
                result[name] = override
            elif name in self._sdk_factories:
                result[name] = self._sdk_factories[name](role, grants)
            elif name in self._servers:
                result[name] = self._servers[name]
            else:
                logger.warning("MCP server '%s' is not registered", name)
        return result
