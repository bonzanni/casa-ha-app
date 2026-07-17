"""Connection-bound Home Assistant voice route registration."""

from __future__ import annotations

import asyncio

import pytest

from channels.voice.routes import VoiceRouteRegistry, VoiceWsConnection
from config import AgentConfig


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _RawSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.active_sends = 0
        self.max_concurrent_send = 0

    async def send_json(self, frame: dict) -> None:
        self.active_sends += 1
        self.max_concurrent_send = max(
            self.max_concurrent_send, self.active_sends,
        )
        await asyncio.sleep(0)
        self.sent.append(frame)
        self.active_sends -= 1


def _cfg(role: str, channels: list[str]) -> AgentConfig:
    return AgentConfig(role=role, channels=channels)


def _register_frame(**changes) -> dict:
    return {
        "type": "voice_route_register",
        "protocol": 1,
        "route_id": "entry-1",
        "agent_role": "concierge",
        "capabilities": ["background_jobs", "satellite_announce"],
        **changes,
    }


async def test_connection_writer_serializes_all_frame_producers():
    raw = _RawSocket()
    connection = VoiceWsConnection(raw)

    await asyncio.gather(
        connection.send_json({"type": "block", "utterance_id": "u1"}),
        connection.send_json({"type": "block", "utterance_id": "u2"}),
        connection.send_json({"type": "job_ready", "job_id": "job-1"}),
    )

    assert len(raw.sent) == 3
    assert raw.max_concurrent_send == 1


async def test_authenticated_registration_is_acknowledged_and_bound_to_socket():
    raw = _RawSocket()
    connection = VoiceWsConnection(raw)
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _cfg("concierge", ["ha_voice"])},
    )

    bound = await routes.register(connection, _register_frame())

    assert raw.sent == [{
        "type": "voice_route_registered",
        "protocol": 1,
        "accepted_capabilities": [
            "background_jobs", "satellite_announce",
        ],
    }]
    assert routes.get_connected("entry-1") is bound
    assert bound is not None
    assert bound.connection is connection
    assert bound.role == "concierge"
    assert connection.voice_route_id == "entry-1"
    assert connection.voice_route_capabilities == frozenset({
        "background_jobs", "satellite_announce",
    })
    assert bound.job_control_id == "entry-1"
    assert connection.voice_job_control_id == "entry-1"


@pytest.mark.parametrize(
    ("secret_present", "frame"),
    [
        (False, _register_frame()),
        (True, _register_frame(route_id="")),
        (True, _register_frame(agent_role="unknown")),
        (True, _register_frame(agent_role="text-only")),
    ],
)
async def test_invalid_registration_accepts_no_capabilities(
    secret_present, frame,
):
    raw = _RawSocket()
    connection = VoiceWsConnection(raw)
    routes = VoiceRouteRegistry(
        secret_present=secret_present,
        agent_configs={
            "concierge": _cfg("concierge", ["ha_voice"]),
            "text-only": _cfg("text-only", ["webhook"]),
        },
    )

    assert await routes.register(connection, frame) is None
    assert raw.sent[-1]["accepted_capabilities"] == []
    assert routes.get_connected("entry-1") is None
    assert connection.voice_route_id is None
    assert connection.voice_job_control_id is None


async def test_unknown_protocol_and_capability_fail_closed_but_are_acknowledged():
    raw = _RawSocket()
    connection = VoiceWsConnection(raw)
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _cfg("concierge", ["ha_voice"])},
    )

    assert await routes.register(
        connection,
        _register_frame(protocol=7, capabilities=["future_capability"]),
    ) is None
    assert raw.sent == [{
        "type": "voice_route_registered",
        "protocol": 7,
        "accepted_capabilities": [],
    }]


@pytest.mark.parametrize("protocol", [True, 1.0])
async def test_protocol_requires_exact_json_integer_one(protocol):
    raw = _RawSocket()
    connection = VoiceWsConnection(raw)
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _cfg("concierge", ["ha_voice"])},
    )

    assert await routes.register(
        connection, _register_frame(protocol=protocol),
    ) is None
    assert raw.sent[-1] == {
        "type": "voice_route_registered",
        "protocol": protocol,
        "accepted_capabilities": [],
    }
    assert routes.get_connected("entry-1") is None


async def test_any_unknown_capability_fails_the_whole_registration_closed():
    raw = _RawSocket()
    connection = VoiceWsConnection(raw)
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _cfg("concierge", ["ha_voice"])},
    )

    assert await routes.register(connection, _register_frame(
        capabilities=["background_jobs", "future_capability"],
    )) is None
    assert raw.sent[-1]["accepted_capabilities"] == []
    assert routes.get_connected("entry-1") is None


async def test_disconnect_clears_writer_but_retains_capability_for_60_seconds():
    now = [100.0]
    connection = VoiceWsConnection(_RawSocket())
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _cfg("concierge", ["ha_voice"])},
        freshness_s=60,
        clock=lambda: now[0],
    )
    await routes.register(connection, _register_frame())

    await routes.disconnect(connection)

    assert routes.get_connected("entry-1") is None
    assert routes.is_recently_capable("entry-1") is True
    now[0] = 160.001
    assert routes.is_recently_capable("entry-1") is False


async def test_expired_route_metadata_is_pruned_before_new_registration():
    now = [100.0]
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _cfg("concierge", ["ha_voice"])},
        freshness_s=60,
        clock=lambda: now[0],
    )
    old = VoiceWsConnection(_RawSocket())
    await routes.register(old, _register_frame(route_id="entry-old"))
    await routes.disconnect(old)

    now[0] = 160.001
    current = VoiceWsConnection(_RawSocket())
    await routes.register(current, _register_frame(route_id="entry-current"))

    assert set(routes._metadata) == {"entry-current"}


async def test_old_socket_disconnect_does_not_clear_new_route_writer():
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _cfg("concierge", ["ha_voice"])},
    )
    old = VoiceWsConnection(_RawSocket())
    new = VoiceWsConnection(_RawSocket())
    await routes.register(old, _register_frame())
    rebound = await routes.register(new, _register_frame())

    await routes.disconnect(old)

    assert routes.get_connected("entry-1") is rebound
