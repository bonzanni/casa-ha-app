"""Eager in-process facade for Home Assistant's dynamic MCP tool surface."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from contextlib import AsyncExitStack
from typing import Any, AsyncContextManager, Callable, Protocol

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientRequest,
    ListToolsRequest,
)
from pydantic import BaseModel, ConfigDict


logger = logging.getLogger(__name__)

LIVE_CONTEXT_TOOL = "GetLiveContext"
LIVE_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"domain": {"type": "string"}},
    "additionalProperties": False,
}
UNAVAILABLE_TEXT = "Home Assistant is temporarily unavailable."

SurfaceDescriptor = tuple[tuple[str, str, str], ...]


class _UpstreamSession(Protocol):
    async def initialize(self) -> Any: ...

    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


SessionFactory = Callable[[], AsyncContextManager[_UpstreamSession]]


class _DiscoveredTool(BaseModel):
    """Tolerant transport shape; schema semantics are checked per tool."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str | None = None
    inputSchema: Any = None


class _DiscoveredTools(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tools: list[_DiscoveredTool]


class HomeAssistantFacade:
    """Own an HA MCP connection and mirror its healthy tools eagerly."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        on_schema_change: Callable[[], Any] | None = None,
        session_factory: SessionFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._headers = dict(headers)
        self._on_schema_change = on_schema_change
        self._session_factory = session_factory
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: _UpstreamSession | None = None
        self._session_generation = 0
        self._tools: tuple[SdkMcpTool[Any], ...] = ()
        self._server_config: dict[str, Any] | None = None
        self._surface_descriptor: SurfaceDescriptor = ()
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_pending = False
        self._closed = False

    @property
    def tools(self) -> tuple[SdkMcpTool[Any], ...]:
        """The exact immutable proxy tuple installed in the SDK server."""
        return self._tools

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(candidate.name for candidate in self._tools)

    @property
    def server_config(self) -> dict[str, Any]:
        if self._server_config is None:
            raise RuntimeError("Home Assistant facade has not been started")
        return self._server_config

    async def start(self) -> None:
        async with self._lock:
            if self._session is not None:
                return
            self._closed = False
            await self._refresh_locked()

    async def refresh(self) -> None:
        """Reconnect, rediscover tools, and publish the refreshed schema."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Home Assistant facade is closed")
            surface_changed = await self._refresh_locked()
        if surface_changed and self._on_schema_change is not None:
            callback_result = self._on_schema_change()
            if inspect.isawaitable(callback_result):
                await callback_result

    async def aclose(self) -> None:
        self._closed = True
        self._refresh_pending = False
        refresh_task = self._refresh_task
        self._refresh_task = None
        if refresh_task is not None and refresh_task is not asyncio.current_task():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            await self._close_upstream_locked()

    async def _refresh_locked(self) -> bool:
        await self._close_upstream_locked()
        stack, session = await self._open_upstream()
        try:
            discovered = await self._discover_tools(session)
            proxies = tuple(
                proxy
                for candidate in discovered
                if (proxy := self._proxy_for(candidate)) is not None
            )
        except BaseException:
            await stack.aclose()
            raise

        config = create_sdk_mcp_server(
            name="homeassistant",
            tools=list(proxies),
        )
        config["alwaysLoad"] = True
        descriptor = _surface_descriptor_for(proxies)
        surface_changed = descriptor != self._surface_descriptor
        self._stack = stack
        self._session = session
        self._session_generation += 1
        self._tools = proxies
        self._server_config = config
        self._surface_descriptor = descriptor
        return surface_changed

    async def _discover_tools(
        self,
        session: _UpstreamSession,
    ) -> list[Any]:
        send_request = getattr(session, "send_request", None)
        if send_request is None:
            return (await session.list_tools()).tools

        result = await send_request(
            ClientRequest(ListToolsRequest(params=None)),
            _DiscoveredTools,
        )
        return result.tools

    async def _open_upstream(
        self,
    ) -> tuple[AsyncExitStack, _UpstreamSession]:
        stack = AsyncExitStack()
        try:
            if self._session_factory is not None:
                session = await stack.enter_async_context(self._session_factory())
            else:
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(self._url, headers=self._headers),
                )
                session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        return stack, session

    async def _close_upstream_locked(self) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.warning(
                    "Home Assistant upstream close failed; detail suppressed",
                )

    def _proxy_for(self, tool_spec: Any) -> SdkMcpTool[Any] | None:
        try:
            schema = _schema_for(tool_spec)
        except ValueError:
            logger.warning(
                "Skipping Home Assistant tool %s: invalid input schema",
                tool_spec.name,
            )
            return None

        @tool(tool_spec.name, tool_spec.description or "", schema)
        async def proxy(arguments: dict[str, Any]) -> dict[str, Any]:
            return await self._call(tool_spec.name, arguments)

        return proxy

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        session = self._session
        session_generation = self._session_generation
        if session is None:
            self._ensure_refresh_worker()
            return _unavailable_result()

        upstream_arguments = {} if name == LIVE_CONTEXT_TOOL else arguments
        started_ms = self._monotonic() * 1000
        try:
            result = await self._call_upstream(
                session,
                name,
                upstream_arguments,
            )
        except Exception:
            elapsed = int(self._monotonic() * 1000 - started_ms)
            logger.info(
                "ha_facade_call tool=%s ok=False ms=%d",
                name,
                elapsed,
            )
            invalidated = await self._disconnect_failed(
                session,
                session_generation,
            )
            if invalidated:
                self._request_follow_up_refresh()
            logger.warning(
                "Home Assistant tool transport failed; detail suppressed",
            )
            return _unavailable_result()
        elapsed = int(self._monotonic() * 1000 - started_ms)
        logger.info(
            "ha_facade_call tool=%s ok=%s ms=%d",
            name,
            not bool(getattr(result, "isError", False)),
            elapsed,
        )
        payload = _sdk_result(result)
        if name != LIVE_CONTEXT_TOOL or not arguments.get("domain"):
            return payload
        return _filter_live_context(payload, arguments["domain"])

    async def _call_upstream(
        self,
        session: _UpstreamSession,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        # MCP Python >=1.28.1,<2 ClientSession.call_tool() performs a hidden
        # strict tools/list rediscovery that can reject the whole HA surface
        # when one dynamic schema is malformed. The typed raw send_request
        # path is intentional because facade discovery already filtered those
        # schemas. On every MCP bump rerun these regressions:
        # test_raw_discovery_calls_healthy_tool_without_strict_rediscovery,
        # TestMockHaMcp.test_live_context_domain_flows_through_facade_as_empty_arguments
        # (real transport), and test_ha_mock_e2e_pins_eager_facade_contract
        # (mock-e2e guard).
        send_request = getattr(session, "send_request", None)
        if send_request is None:
            return await session.call_tool(name, arguments)
        return await send_request(
            ClientRequest(CallToolRequest(
                params=CallToolRequestParams(
                    name=name,
                    arguments=arguments,
                ),
            )),
            CallToolResult,
        )

    async def _disconnect_failed(
        self,
        failed_session: _UpstreamSession,
        failed_generation: int,
    ) -> bool:
        async with self._lock:
            if (
                self._session is not failed_session
                or self._session_generation != failed_generation
            ):
                return False
            await self._close_upstream_locked()
            return True

    def _ensure_refresh_worker(self) -> None:
        """Start recovery unless an existing worker already owns it."""
        if self._closed:
            return
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._run_scheduled_refresh(),
                name="ha-mcp-facade-refresh",
            )

    def _request_follow_up_refresh(self) -> None:
        """Retain recovery requested while the worker is already in flight."""
        if self._closed:
            return
        self._refresh_pending = True
        self._ensure_refresh_worker()

    async def _run_scheduled_refresh(self) -> None:
        while True:
            self._refresh_pending = False
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Home Assistant schema refresh failed; detail suppressed",
                )
            if not self._refresh_pending:
                return


def _schema_for(tool_spec: Any) -> dict[str, Any]:
    if tool_spec.name == LIVE_CONTEXT_TOOL:
        return {
            **LIVE_CONTEXT_SCHEMA,
            "properties": dict(LIVE_CONTEXT_SCHEMA["properties"]),
        }

    schema = tool_spec.inputSchema
    if not isinstance(schema, dict):
        raise ValueError("inputSchema is not an object")
    normalized = dict(schema)
    normalized.setdefault("type", "object")
    if normalized["type"] != "object":
        raise ValueError("inputSchema root must be object")
    normalized.setdefault("properties", {})
    if not isinstance(normalized["properties"], dict):
        raise ValueError("inputSchema properties must be object")
    normalized["properties"] = dict(normalized["properties"])
    return normalized


def _surface_descriptor_for(
    proxies: tuple[SdkMcpTool[Any], ...],
) -> SurfaceDescriptor:
    return tuple(
        (
            proxy.name,
            proxy.description,
            json.dumps(
                proxy.input_schema,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        )
        for proxy in proxies
    )


def _unavailable_result() -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": UNAVAILABLE_TEXT}],
        "is_error": True,
    }


def _sdk_result(result: Any) -> dict[str, Any]:
    payload = {
        "content": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in result.content
        ],
    }
    if result.isError:
        payload["is_error"] = True
    return payload


def _filter_live_context(
    payload: dict[str, Any],
    domain: str,
) -> dict[str, Any]:
    filtered_content = [dict(item) for item in payload["content"]]
    input_count = 0
    output_count = 0
    object_count = 0

    for item in filtered_content:
        if item.get("type") != "text":
            continue
        try:
            decoded = json.loads(item["text"])
        except (KeyError, TypeError, json.JSONDecodeError):
            logger.info(
                "Home Assistant live-context filter unchanged: "
                "content_count=%d error_kind=json_parse",
                len(filtered_content),
            )
            return payload
        if not isinstance(decoded, dict):
            logger.info(
                "Home Assistant live-context filter unchanged: "
                "content_count=%d error_kind=json_shape",
                len(filtered_content),
            )
            return payload

        selected = {
            key: value
            for key, value in decoded.items()
            if key.partition(".")[0] == domain
        }
        item["text"] = json.dumps(selected)
        input_count += len(decoded)
        output_count += len(selected)
        object_count += 1

    logger.info(
        "Home Assistant live-context filter applied: "
        "object_count=%d input_count=%d output_count=%d",
        object_count,
        input_count,
        output_count,
    )
    return {**payload, "content": filtered_content}
