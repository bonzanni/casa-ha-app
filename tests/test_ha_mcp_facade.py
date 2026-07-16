"""Tests for the eager in-process Home Assistant MCP facade."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Callable

import pytest
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    Implementation,
    InitializeResult,
    ListToolsResult,
    ServerCapabilities,
    TextContent,
    Tool,
    ToolsCapability,
)

from ha_mcp_facade import HomeAssistantFacade


pytestmark = pytest.mark.unit


class FakeHaSession:
    """Complete MCP-session boundary fake with typed protocol results."""

    def __init__(
        self,
        *,
        tools: list[Tool],
        results: dict[str, CallToolResult | BaseException] | None = None,
    ) -> None:
        self._tools = tools
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> InitializeResult:
        return InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=ServerCapabilities(tools=ToolsCapability()),
            serverInfo=Implementation(name="fake-ha", version="1.0"),
        )

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(tools=self._tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        outcome = self._results.get(name)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        return text_result('{"success":true}')


class RawDiscoverySession(FakeHaSession):
    """Mirror the pinned client's whole-list validation boundary."""

    def __init__(self, raw_tools: list[dict[str, Any]]) -> None:
        super().__init__(tools=[])
        self._raw_tools = raw_tools

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult.model_validate({"tools": self._raw_tools})

    async def send_request(self, _request: Any, result_type: Any) -> Any:
        return result_type.model_validate({"tools": self._raw_tools})


@asynccontextmanager
async def fake_connection(session: FakeHaSession):
    yield session


class SessionSequence:
    """Open one complete fake MCP session per facade connection."""

    def __init__(self, *sessions: FakeHaSession) -> None:
        self.sessions = sessions
        self.open_count = 0

    def __call__(self):
        @asynccontextmanager
        async def connection():
            session = self.sessions[self.open_count]
            self.open_count += 1
            yield session

        return connection()


class CoordinatedFailingSession(FakeHaSession):
    """Fail two in-flight calls together to exercise reconnect deduplication."""

    def __init__(self, *, tools: list[Tool]) -> None:
        super().__init__(tools=tools)
        self._both_started = asyncio.Event()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if len(self.calls) == 2:
            self._both_started.set()
        await self._both_started.wait()
        raise ConnectionError("secret-bearing transport detail")


class StaggeredFailingSession(FakeHaSession):
    """Let one old-generation failure arrive after recovery completes."""

    def __init__(self, *, tools: list[Tool]) -> None:
        super().__init__(tools=tools)
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if arguments["name"] == "slow":
            self.slow_started.set()
            await self.release_slow.wait()
        else:
            await self.slow_started.wait()
        raise ConnectionError("secret-bearing staggered detail")


class SchemaChangeRecorder:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


async def wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def make_facade(
    session: FakeHaSession | SessionSequence,
    *,
    on_schema_change: SchemaChangeRecorder | None = None,
) -> HomeAssistantFacade:
    session_factory = (
        session if isinstance(session, SessionSequence)
        else lambda: fake_connection(session)
    )
    return HomeAssistantFacade(
        "http://ha/mcp",
        {"Authorization": "Bearer secret"},
        on_schema_change=on_schema_change,
        session_factory=session_factory,
    )


def text_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def action_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"Action {name}",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
    )


def live_context_tool() -> Tool:
    return Tool(
        name="GetLiveContext",
        description="Current Home Assistant state",
        inputSchema={},
    )


async def invoke_sdk_tool(
    server_config: dict[str, Any],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Invoke through the real low-level SDK server request handler."""
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    wrapped = await server_config["instance"].request_handlers[CallToolRequest](
        request,
    )
    result = wrapped.root
    payload = {
        "content": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in result.content
        ],
    }
    if result.isError:
        payload["is_error"] = True
    return payload


@pytest.mark.asyncio
async def test_discovers_all_healthy_tools_and_normalizes_live_context():
    upstream = FakeHaSession(
        tools=[action_tool("HassTurnOff"), live_context_tool()],
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        by_name = {candidate.name: candidate for candidate in facade.tools}
        assert set(by_name) == {"HassTurnOff", "GetLiveContext"}
        assert by_name["GetLiveContext"].input_schema == {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "additionalProperties": False,
        }
        assert facade.tool_names == ("HassTurnOff", "GetLiveContext")
        assert facade.server_config["alwaysLoad"] is True
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_one_bad_schema_omits_only_that_tool(caplog):
    broken = Tool.model_construct(
        name="Broken",
        description="Bad schema",
        inputSchema="not-an-object",
    )
    upstream = FakeHaSession(
        tools=[broken, action_tool("HassTurnOn")],
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        assert facade.tool_names == ("HassTurnOn",)
        assert "Broken" in caplog.text
        assert "not-an-object" not in caplog.text
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_raw_discovery_omits_bad_schema_without_losing_healthy_tool():
    upstream = RawDiscoverySession([
        {
            "name": "Broken",
            "description": "Bad schema",
            "inputSchema": "not-an-object",
        },
        {
            "name": "HassTurnOn",
            "description": "Turn on",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ])
    facade = make_facade(upstream)

    await facade.start()
    try:
        assert facade.tool_names == ("HassTurnOn",)
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_live_context_domain_is_never_forwarded_upstream():
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={
            "GetLiveContext": text_result(json.dumps({
                "light.kitchen": "on",
                "climate.office": "idle",
            })),
        },
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "GetLiveContext",
            {"domain": "light"},
        )
        assert upstream.calls == [("GetLiveContext", {})]
        assert json.loads(result["content"][0]["text"]) == {
            "light.kitchen": "on",
        }
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_unparseable_live_context_is_returned_unchanged():
    raw = "Kitchen light: on\nOffice climate: idle"
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={"GetLiveContext": text_result(raw)},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "GetLiveContext",
            {"domain": "light"},
        )
        assert upstream.calls == [("GetLiveContext", {})]
        assert result["content"][0]["text"] == raw
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_parseable_non_object_live_context_is_returned_unchanged():
    raw = "[]"
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={"GetLiveContext": text_result(raw)},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "GetLiveContext",
            {"domain": "light"},
        )
        assert upstream.calls == [("GetLiveContext", {})]
        assert result["content"][0]["text"] == raw
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_action_proxy_preserves_arguments_exactly():
    upstream = FakeHaSession(
        tools=[action_tool("HassTurnOff")],
        results={"HassTurnOff": text_result('{"success":true}')},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        await invoke_sdk_tool(
            facade.server_config,
            "HassTurnOff",
            {"name": "office light"},
        )
        assert upstream.calls == [
            ("HassTurnOff", {"name": "office light"}),
        ]
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_transport_failure_returns_fixed_error_and_refreshes_once(caplog):
    failed = FakeHaSession(
        tools=[action_tool("HassTurnOn")],
        results={
            "HassTurnOn": ConnectionError(
                "secret-bearing transport detail",
            ),
        },
    )
    healthy = FakeHaSession(tools=[action_tool("HassTurnOn")])
    sessions = SessionSequence(failed, healthy)
    changed = SchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        original_config = facade.server_config
        result = await invoke_sdk_tool(
            original_config,
            "HassTurnOn",
            {"name": "private office"},
        )
        assert result == {
            "content": [{
                "type": "text",
                "text": "Home Assistant is temporarily unavailable.",
            }],
            "is_error": True,
        }
        await wait_until(
            lambda: sessions.open_count == 2
            and facade.server_config is not original_config,
        )
        assert changed.count == 0
        assert sessions.open_count == 2
        assert failed.calls == [
            ("HassTurnOn", {"name": "private office"}),
        ]
        assert healthy.calls == []
        assert "Bearer secret" not in caplog.text
        assert "secret-bearing transport detail" not in caplog.text
        assert "private office" not in caplog.text
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_concurrent_transport_failures_share_one_reconnect():
    failed = CoordinatedFailingSession(
        tools=[action_tool("HassTurnOn")],
    )
    healthy = FakeHaSession(tools=[action_tool("HassTurnOn")])
    sessions = SessionSequence(failed, healthy)
    changed = SchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        original_config = facade.server_config
        results = await asyncio.gather(
            invoke_sdk_tool(original_config, "HassTurnOn", {"name": "one"}),
            invoke_sdk_tool(original_config, "HassTurnOn", {"name": "two"}),
        )
        assert all(result["is_error"] is True for result in results)
        await wait_until(
            lambda: sessions.open_count == 2
            and facade.server_config is not original_config,
        )
        assert changed.count == 0
        assert sessions.open_count == 2
        assert len(failed.calls) == 2
        assert healthy.calls == []
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_new_tool_appears_after_refresh_without_losing_healthy_tools():
    sessions = SessionSequence(
        FakeHaSession(tools=[action_tool("HassTurnOn")]),
        FakeHaSession(tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
        ]),
    )
    changed = SchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        await facade.refresh()
        assert facade.tool_names == ("HassTurnOn", "HassLightSet")
        assert sessions.open_count == 2
        assert changed.count == 1
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_identical_normalized_surface_does_not_notify_schema_change():
    initial = Tool(
        name="HassTurnOn",
        description="Turn on",
        inputSchema={},
    )
    equivalent = Tool(
        name="HassTurnOn",
        description="Turn on",
        inputSchema={"type": "object", "properties": {}},
    )
    sessions = SessionSequence(
        FakeHaSession(tools=[initial]),
        FakeHaSession(tools=[equivalent]),
    )
    changed = SchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        original_config = facade.server_config
        await facade.refresh()
        assert facade.server_config is not original_config
        assert changed.count == 0
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_stale_generation_failure_does_not_replace_recovered_session():
    failed = StaggeredFailingSession(
        tools=[action_tool("HassTurnOn")],
    )
    healthy = FakeHaSession(tools=[action_tool("HassTurnOn")])
    replacement = FakeHaSession(tools=[action_tool("HassTurnOn")])
    sessions = SessionSequence(failed, healthy, replacement)
    facade = make_facade(sessions)

    await facade.start()
    try:
        original_config = facade.server_config
        slow_call = asyncio.create_task(
            invoke_sdk_tool(
                original_config,
                "HassTurnOn",
                {"name": "slow"},
            ),
        )
        await asyncio.wait_for(failed.slow_started.wait(), timeout=1)

        fast_result = await invoke_sdk_tool(
            original_config,
            "HassTurnOn",
            {"name": "fast"},
        )
        assert fast_result["is_error"] is True
        await wait_until(
            lambda: sessions.open_count == 2
            and facade.server_config is not original_config,
        )
        recovered_config = facade.server_config

        failed.release_slow.set()
        slow_result = await slow_call
        assert slow_result["is_error"] is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sessions.open_count == 2
        await invoke_sdk_tool(
            recovered_config,
            "HassTurnOn",
            {"name": "healthy"},
        )
        assert healthy.calls == [("HassTurnOn", {"name": "healthy"})]
        assert replacement.calls == []
    finally:
        failed.release_slow.set()
        await facade.aclose()
