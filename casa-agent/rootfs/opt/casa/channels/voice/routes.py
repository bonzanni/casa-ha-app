"""Authenticated, connection-bound Home Assistant voice routes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from channel_authz import agent_allowed_on


_PROTOCOL = 1
_CAPABILITIES = ("background_jobs", "satellite_announce")
_BACKGROUND_CAPABILITIES = frozenset(_CAPABILITIES)


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        return None
    return normalized


def _is_protocol_one(value: Any) -> bool:
    return type(value) is int and value == _PROTOCOL


class VoiceWsConnection:
    """One websocket and the only JSON writer allowed to use it."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._send_lock = asyncio.Lock()
        # These fields are written only by VoiceRouteRegistry.
        self.voice_route_id: str | None = None
        self.voice_route_role: str | None = None
        self.voice_route_capabilities: frozenset[str] = frozenset()

    async def send_json(self, frame: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._ws.send_json(frame)


@dataclass(frozen=True)
class BoundVoiceRoute:
    route_id: str
    role: str
    capabilities: frozenset[str]
    connection: VoiceWsConnection
    connected_at: float

    async def send_json(self, frame: dict[str, Any]) -> None:
        await self.connection.send_json(frame)


@dataclass
class _RouteMetadata:
    role: str
    capabilities: frozenset[str]
    last_seen: float


class VoiceRouteRegistry:
    """Server-owned route identity and short disconnect freshness metadata."""

    def __init__(
        self,
        secret_present: bool,
        freshness_s: float = 60,
        *,
        agent_configs: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._secret_present = bool(secret_present)
        self._freshness_s = float(freshness_s)
        self._agent_configs = agent_configs if agent_configs is not None else {}
        self._clock = clock
        self._connected: dict[str, BoundVoiceRoute] = {}
        self._metadata: dict[str, _RouteMetadata] = {}

    async def register(
        self,
        connection: VoiceWsConnection,
        frame: Mapping[str, Any],
    ) -> BoundVoiceRoute | None:
        """Validate, acknowledge, and bind a protocol-1 registration."""
        self._clear_connection_binding(connection)
        protocol = frame.get("protocol")
        accepted: tuple[str, ...] = ()
        bound: BoundVoiceRoute | None = None

        route_id = _identifier(frame.get("route_id"))
        role = _identifier(frame.get("agent_role"))
        cfg = self._agent_configs.get(role) if role is not None else None
        requested = frame.get("capabilities")
        valid_requested = isinstance(requested, (list, tuple, set, frozenset))
        requested_set = frozenset()
        if valid_requested:
            valid_requested = all(
                isinstance(capability, str) for capability in requested
            )
            requested_set = frozenset(requested)
            valid_requested = (
                valid_requested
                and requested_set <= _BACKGROUND_CAPABILITIES
            )

        if (
            _is_protocol_one(protocol)
            and self._secret_present
            and route_id is not None
            and role is not None
            and cfg is not None
            and agent_allowed_on("voice", cfg)
            and valid_requested
        ):
            accepted = tuple(
                capability
                for capability in _CAPABILITIES
                if capability in requested_set
            )
            capabilities = frozenset(accepted)
            now = self._clock()
            old = self._connected.get(route_id)
            if old is not None and old.connection is not connection:
                self._clear_connection_fields(old.connection)
            bound = BoundVoiceRoute(
                route_id=route_id,
                role=role,
                capabilities=capabilities,
                connection=connection,
                connected_at=now,
            )
            self._connected[route_id] = bound
            self._metadata[route_id] = _RouteMetadata(
                role=role,
                capabilities=capabilities,
                last_seen=now,
            )
            connection.voice_route_id = route_id
            connection.voice_route_role = role
            connection.voice_route_capabilities = capabilities

        await connection.send_json({
            "type": "voice_route_registered",
            "protocol": protocol,
            "accepted_capabilities": list(accepted),
        })
        return bound

    def get_connected(self, route_id: str) -> BoundVoiceRoute | None:
        return self._connected.get(route_id)

    def is_recently_capable(self, route_id: str) -> bool:
        connected = self._connected.get(route_id)
        if connected is not None:
            return _BACKGROUND_CAPABILITIES <= connected.capabilities
        metadata = self._metadata.get(route_id)
        return bool(
            metadata is not None
            and _BACKGROUND_CAPABILITIES <= metadata.capabilities
            and self._clock() - metadata.last_seen <= self._freshness_s
        )

    def touch(self, connection: VoiceWsConnection) -> None:
        route_id = connection.voice_route_id
        if route_id is None:
            return
        connected = self._connected.get(route_id)
        if connected is None or connected.connection is not connection:
            return
        metadata = self._metadata.get(route_id)
        if metadata is not None:
            metadata.last_seen = self._clock()

    async def disconnect(
        self, connection: VoiceWsConnection,
    ) -> BoundVoiceRoute | None:
        route_id = connection.voice_route_id
        if route_id is None:
            return None
        connected = self._connected.get(route_id)
        if connected is None or connected.connection is not connection:
            self._clear_connection_fields(connection)
            return None
        self._connected.pop(route_id, None)
        metadata = self._metadata.get(route_id)
        if metadata is not None:
            metadata.last_seen = self._clock()
        self._clear_connection_fields(connection)
        return connected

    def _clear_connection_binding(self, connection: VoiceWsConnection) -> None:
        route_id = connection.voice_route_id
        if route_id is not None:
            connected = self._connected.get(route_id)
            if connected is not None and connected.connection is connection:
                self._connected.pop(route_id, None)
                metadata = self._metadata.get(route_id)
                if metadata is not None:
                    metadata.last_seen = self._clock()
        self._clear_connection_fields(connection)

    @staticmethod
    def _clear_connection_fields(connection: VoiceWsConnection) -> None:
        connection.voice_route_id = None
        connection.voice_route_role = None
        connection.voice_route_capabilities = frozenset()


__all__ = ["BoundVoiceRoute", "VoiceRouteRegistry", "VoiceWsConnection"]
