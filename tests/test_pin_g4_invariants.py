"""Pinning tests for configuration/trigger/MCP invariants (docs corpus).

Each test names the corpus invariant it pins and records, in its docstring, the
red case that was demonstrated: the code edit that made it fail. A pinning test
never shown red proves nothing.
"""
import json
from pathlib import Path

import jsonschema
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import reload as reload_mod
from internal_handlers import _make_internal_tools_call_handler


def test_pin_inv_cfg_001_exactly_eight_scopes_none_reading_options():
    """INV-CFG-001: exactly eight reload scopes exist, and none rereads the
    app manifest options.

    Red case demonstrated: registering a ninth scope
    (`register_handler("options", ...)`) fails the set equality.
    """
    import inspect

    assert set(reload_mod._HANDLERS) == {
        "triggers", "agent", "policies", "plugin_env",
        "agents", "executors", "config_sync", "full",
    }
    # "Rereads the app manifest options" means the Supervisor options store —
    # /data/options.json or a bashio read — not os.environ generally (the
    # plugin_env scope legitimately WRITES env when re-sourcing its conf).
    for handler in reload_mod._HANDLERS.values():
        source = inspect.getsource(handler)
        assert "options.json" not in source
        assert "bashio" not in source


def test_pin_inv_trig_002_plugin_namespace_reserved_in_user_schema():
    """INV-TRIG-002 (namespace half): the user trigger schema refuses the
    plugin prefix, and a plugin declaration produces exactly that prefix, so
    the namespaces cannot collide. Uniqueness is pinned by the trigger
    registry's duplicate/cross-role tests.

    Red case demonstrated: removing the `(?!plg-)` guard from the schema's
    name pattern accepts the plugin-shaped user name and this test fails.
    """
    from plugin_triggers import parse_and_validate

    schema = json.loads(Path(
        "casa/rootfs/opt/casa/defaults/schema/triggers.v1.json"
    ).read_text())
    name_pattern = (
        schema["properties"]["triggers"]["items"]["properties"]["name"]["pattern"]
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate("plg-p--x", {"type": "string", "pattern": name_pattern})

    triggers, errors = parse_and_validate("p", {"casa": {"triggers": [{
        "name": "x", "type": "webhook", "target": "resident:assistant",
        "auth": {"mode": "static_header"},
    }]}})
    assert errors == []
    assert triggers[0]["effective"] == "plg-p--x"


async def test_pin_inv_mcp_001_internal_dispatch_enforces_engagement_grant():
    """INV-MCP-001 (v0.166.0, inverted): internal tool dispatch enforces the
    authenticated engagement's grant — an engagement that does not grant a tool
    cannot invoke it over the bridge, and an unbound call fails closed.

    The old invariant (dispatch ignores the allowlist, "enforcement happens
    before dispatch") rested on INV-MCP-002 — that only the trusted CLI reaches
    the socket. That is false for a broad-Bash executor: its own root shell can
    read the workspace token and POST the socket directly, bypassing the
    CLI-side allowlist. So enforcement moved to dispatch.

    Red case demonstrated: removing the grant check from the internal
    tools-call handler makes the ungranted call below dispatch, failing this
    test.
    """
    async def privileged(_arguments):
        return {"content": [{"type": "text", "text": "ran"}]}

    def _record(tools_allowed):
        return type("Record", (), {
            "status": "active", "kind": "executor",
            "tools_allowed": tools_allowed,
            "auth_token": "tok-e1",  # #335: id claims bind only with the token
        })()

    def _app(record):
        registry = type("Registry", (), {"get": lambda self, _id: record})()
        app = web.Application()
        app.router.add_post(
            "/internal/tools/call",
            _make_internal_tools_call_handler(
                tool_dispatch={"privileged": privileged},
                engagement_registry=registry,
            ),
        )
        return app

    # Not granted → rejected, tool never runs.
    async with TestClient(TestServer(_app(_record(())))) as client:
        response = await client.post("/internal/tools/call", json={
            "name": "privileged", "arguments": {}, "engagement_id": "e1",
            "engagement_token": "tok-e1",
        })
        payload = await response.json()
    assert "tool_not_granted" in payload["error"]["message"]

    # Granted → dispatches.
    granted = ("mcp__casa-framework__privileged",)
    async with TestClient(TestServer(_app(_record(granted)))) as client:
        response = await client.post("/internal/tools/call", json={
            "name": "privileged", "arguments": {}, "engagement_id": "e1",
            "engagement_token": "tok-e1",
        })
        payload = await response.json()
    assert payload["content"] == [{"type": "text", "text": "ran"}]
