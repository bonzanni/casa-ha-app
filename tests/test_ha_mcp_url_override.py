"""Contracts for the HA MCP URL and Tina facade boot wiring.

The URL tests inline the env-read pattern that `casa_core.main` uses. Facade
tests exercise the extracted boot/lifecycle helpers without booting the full
app, while a narrow source assertion pins their ownership and order in main.
The Phase F e2e
(`test-local/e2e/test_ha_delegation.sh`) covers the live wiring by
asserting the addon log line `Registered Home Assistant MCP server (url=...)`
contains the override URL."""
from __future__ import annotations

import inspect
import logging
import os
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def recording_facade(monkeypatch):
    import casa_core

    class RecordingFacade:
        instances = []
        start_error = None

        def __init__(self, url, headers, on_schema_change=None):
            self.url = url
            self.headers = dict(headers)
            self.on_schema_change = on_schema_change
            self.server_config = {"type": "sdk", "instance": self}
            self.started = False
            self.closed = False
            self.__class__.instances.append(self)

        async def start(self):
            self.started = True
            if self.__class__.start_error is not None:
                raise self.__class__.start_error

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(
        casa_core, "HomeAssistantFacade", RecordingFacade, raising=False,
    )
    return RecordingFacade


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


async def test_start_tina_facade_uses_supervisor_auth_and_butler_override(
    recording_facade,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http(
        "homeassistant", "http://raw",
        headers={"Authorization": "Bearer secret-token"},
    )

    facade = await _start_tina_ha_facade(
        registry,
        {"butler": SimpleNamespace(channels=["ha_voice"])},
        {},
        ha_mcp_url="http://raw",
        supervisor_token="secret-token",
        env={},
    )

    assert facade is recording_facade.instances[0]
    assert facade.started
    assert facade.url == "http://raw"
    assert facade.headers == {"Authorization": "Bearer secret-token"}
    assert callable(facade.on_schema_change)
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"] is facade.server_config
    assert registry.resolve(
        ["homeassistant"], role="assistant",
    )["homeassistant"]["url"] == "http://raw"


async def test_tina_facade_callback_uses_current_agents_and_refreshed_config(
    recording_facade,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")
    agents = {}
    facade = await _start_tina_ha_facade(
        registry,
        {"butler": SimpleNamespace(channels=["ha_voice"])},
        agents,
        ha_mcp_url="http://raw",
        supervisor_token="secret-token",
        env={},
    )

    class ObservingAgent:
        def __init__(self):
            self.seen = []

        async def invalidate_tool_surface(self):
            self.seen.append(
                registry.resolve(["homeassistant"], role="butler")[
                    "homeassistant"
                ]
            )

    butler = ObservingAgent()
    agents["butler"] = butler
    refreshed = {"type": "sdk", "instance": object()}
    facade.server_config = refreshed

    await facade.on_schema_change()

    assert butler.seen == [refreshed]
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"] is refreshed
    assert registry.resolve(
        ["homeassistant"], role="assistant",
    )["homeassistant"]["url"] == "http://raw"


async def test_tina_facade_false_kill_switch_logs_once_without_secrets(
    recording_facade, caplog,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://private-ha")

    with caplog.at_level(logging.INFO):
        facade = await _start_tina_ha_facade(
            registry,
            {"butler": SimpleNamespace(channels=["ha_voice"])},
            {},
            ha_mcp_url="http://private-ha",
            supervisor_token="private-token",
            env={"TINA_HA_FACADE_ENABLED": "false"},
        )

    assert facade is None
    assert recording_facade.instances == []
    assert [
        record.getMessage() for record in caplog.records
        if "ha_facade" in record.getMessage()
    ] == ["ha_facade_disabled"]
    assert "private-token" not in caplog.text
    assert "private-ha" not in caplog.text
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"]["url"] == "http://private-ha"


@pytest.mark.parametrize(
    ("supervisor_token", "role_configs"),
    [
        ("", {"butler": SimpleNamespace(channels=["ha_voice"])}),
        ("secret-token", {}),
        ("secret-token", {"butler": SimpleNamespace(channels=["telegram"])}),
    ],
    ids=["no-token", "no-butler", "butler-without-ha-voice"],
)
async def test_tina_facade_requires_token_and_ha_voice_butler(
    recording_facade, supervisor_token, role_configs,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")

    facade = await _start_tina_ha_facade(
        registry,
        role_configs,
        {},
        ha_mcp_url="http://raw",
        supervisor_token=supervisor_token,
        env={},
    )

    assert facade is None
    assert recording_facade.instances == []
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"]["url"] == "http://raw"


async def test_tina_facade_initial_discovery_failure_is_sanitized_degraded(
    recording_facade, caplog,
):
    from casa_core import _start_tina_ha_facade
    from mcp_registry import McpServerRegistry

    recording_facade.start_error = RuntimeError(
        "private-token failed at http://private-ha",
    )
    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")

    with caplog.at_level(logging.WARNING):
        facade = await _start_tina_ha_facade(
            registry,
            {"butler": SimpleNamespace(channels=["ha_voice"])},
            {},
            ha_mcp_url="http://private-ha",
            supervisor_token="private-token",
            env={},
        )

    assert facade is None
    assert recording_facade.instances[0].closed
    assert [
        record.getMessage() for record in caplog.records
        if "ha_facade" in record.getMessage()
    ] == ["ha_facade_initialization_failed status=degraded"]
    assert "private-token" not in caplog.text
    assert "private-ha" not in caplog.text
    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"]["url"] == "http://raw"


def test_main_owns_tina_facade_boot_and_shutdown_lifecycle():
    from casa_core import main

    source = inspect.getsource(main)
    assert "ha_facade = await _start_tina_ha_facade(" in source
    assert "await _close_tina_ha_facade(ha_facade)" in source
    assert source.index("agents[role] = agent") < source.index(
        "ha_facade = await _start_tina_ha_facade(",
    )
