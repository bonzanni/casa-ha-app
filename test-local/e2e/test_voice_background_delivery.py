"""Real Casa ↔ HA integration background-delivery protocol acceptance."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from ha_integration_stubs import HA_STUB_EXPORTS, install


install()


def _integration_root() -> Path:
    configured = os.environ.get("CASA_HA_INTEGRATION_PATH")
    candidates = [] if configured is None else [Path(configured)]
    candidates.extend(
        parent / "casa-ha-integration"
        for parent in Path(__file__).resolve().parents
    )
    for candidate in candidates:
        if (candidate / "custom_components/casa/delivery.py").is_file():
            return candidate.resolve()
    pytest.skip(
        "casa-ha-integration checkout with background delivery is unavailable",
        allow_module_level=True,
    )


INTEGRATION_ROOT = _integration_root()
sys.path.insert(0, str(INTEGRATION_ROOT))
CASA_CODE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "casa-agent/rootfs/opt/casa"
)
sys.path.insert(0, str(CASA_CODE_ROOT))

_conftest_spec = importlib.util.spec_from_file_location(
    "_casa_integration_stub_manifest",
    INTEGRATION_ROOT / "tests/conftest.py",
)
assert _conftest_spec is not None and _conftest_spec.loader is not None
_conftest = importlib.util.module_from_spec(_conftest_spec)
_conftest_spec.loader.exec_module(_conftest)

from custom_components.casa.api import CasaApiClient  # noqa: E402
from custom_components.casa.delivery import (  # noqa: E402
    BackgroundDeliveryManager,
    SatelliteDirectory,
)

import agent as agent_mod  # noqa: E402
import tools  # noqa: E402
from bus import BusMessage, MessageBus, MessageType  # noqa: E402
from channels import ChannelManager  # noqa: E402
from channels.voice.channel import VoiceChannel  # noqa: E402
from channels.voice.delivery import VoiceDeliveryCoordinator  # noqa: E402
from config import AgentConfig, CharacterConfig, DelegateEntry  # noqa: E402
from job_registry import DeliveryState, ExecutionState, JobRegistry  # noqa: E402
from specialist_limits import SpecialistLimiter  # noqa: E402
from specialist_registry import SpecialistRegistry  # noqa: E402


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _eventually(predicate, *, timeout: float = 3.5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _tool_payload(envelope: dict) -> dict:
    return json.loads(envelope["content"][0]["text"])


class _SpecialistRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.outputs: asyncio.Queue[tools.DelegatedOutput] = asyncio.Queue()

    async def __call__(
        self, cfg, task_text, context_text, resolution=None, output_format=None,
    ) -> tools.DelegatedOutput:
        self.calls.append({
            "task": task_text,
            "context": context_text,
            "output_format": output_format,
        })
        return await self.outputs.get()

    async def finish(
        self,
        *,
        spoken: str = "The ruling is no.",
        answer: str = "PRIVATE_ANSWER_CANARY",
        citation: str = "PRIVATE_CITATION_CANARY",
        sensitivity: str = "household",
    ) -> None:
        await self.outputs.put(tools.DelegatedOutput(
            text="PRIVATE_SPECIALIST_TEXT_CANARY",
            structured_output={
                "status": "answered",
                "spoken_summary": spoken,
                "answer": answer,
                "clarification": "",
                "citations": [citation],
                "assumptions": [],
                "provenance": {},
                "sensitivity": sensitivity,
                "delivery_ttl_s": 900,
            },
        ))


class _PoolProbe:
    def __init__(self) -> None:
        self.turns = 0

    def stats(self) -> dict:
        return {"turns": self.turns, "result_tokens": 0, "transcript_tokens": 0}


class _GaryProbe:
    def __init__(self) -> None:
        self._pool = _PoolProbe()
        self.accepted: asyncio.Queue[dict] = asyncio.Queue()

    async def handle_message(self, msg: BusMessage) -> BusMessage:
        self._pool.turns += 1
        on_token = msg.context.get("_on_token")
        if msg.content.startswith("delegate:"):
            _, agent, task = msg.content.split(":", 2)
            origin = {
                "role": "concierge",
                "execution_role": "concierge",
                "channel": "voice",
                "chat_id": msg.context["chat_id"],
                "user_id": None,
                "cid": msg.context["cid"],
                "user_text": task,
                "voice_transport": msg.context.get("_voice_transport"),
                "voice_route_id": msg.context.get("_voice_route_id"),
                "voice_route_capabilities": msg.context.get(
                    "_voice_route_capabilities", frozenset(),
                ),
                "origin_device_id": msg.context.get("_origin_device_id"),
                "_progress_sink": msg.context.get("_progress_sink"),
            }
            token = agent_mod.origin_var.set(origin)
            try:
                result = await tools.delegate_to_agent.handler({
                    "agent": agent,
                    "task": task,
                    "context": "",
                    "mode": "async",
                })
            finally:
                agent_mod.origin_var.reset(token)
            await self.accepted.put(_tool_payload(result))
        if on_token is not None:
            await on_token(
                "Got it; I will bring back the specialist's answer."
                if msg.content.startswith("delegate:")
                else "Still listening."
            )
        return BusMessage(
            type=MessageType.RESPONSE,
            source="concierge",
            target=msg.source,
            content="Accepted.",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )


class _Stack:
    def __init__(self) -> None:
        self.announces: list[dict] = []

    async def submit(
        self,
        *,
        task: str = "Does this target?",
        device_id: str = "dev-k",
        agent: str = "judge",
    ) -> tuple[dict, float]:
        started = time.monotonic()
        frames = []
        async for frame in self.api.stream_utterance(
            text=f"delegate:{agent}:{task}",
            agent_role="concierge",
            scope_id="scope-1",
            utterance_id=f"submit-{time.monotonic_ns()}",
            context={"device_id": device_id},
        ):
            frames.append(frame.kind)
        assert frames[-1] == "done"
        accepted = await asyncio.wait_for(self.gary.accepted.get(), timeout=1)
        return accepted, time.monotonic() - started

    async def second_utterance(self, utterance_id: str = "utterance-2") -> list[str]:
        frames = []
        async for frame in self.api.stream_utterance(
            text="What else can I do?",
            agent_role="concierge",
            scope_id="scope-1",
            utterance_id=utterance_id,
            context={"device_id": "dev-k"},
        ):
            frames.append(frame.kind)
        return frames

    async def reconnect(self) -> None:
        old_generation = self.api._ws_generation
        await self.api._ws.close()
        await _eventually(
            lambda: (
                self.api._ws_generation > old_generation
                and self.api.background_capable
            ),
        )

    async def close(self) -> None:
        await self.manager.close()
        await self.api.close()
        await self.session.close()
        await self.coordinator.stop()
        await self.registry.close()
        self.bus_loop.cancel()
        await asyncio.gather(self.bus_loop, return_exceptions=True)
        await self.runner.cleanup()


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    assert HA_STUB_EXPORTS == _conftest.HA_STUB_EXPORTS
    result = _Stack()
    result.registry = JobRegistry(
        tmp_path / "jobs.json", tmp_path / "delegations.json",
    )
    await result.registry.load()
    result.specialists = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=result.registry,
    )
    caller = AgentConfig(role="concierge", channels=["ha_voice"])
    caller.delegates = [
        DelegateEntry(agent="judge", purpose="rules", when="rules question"),
        DelegateEntry(agent="health", purpose="health", when="health question"),
    ]
    judge = AgentConfig(
        role="judge",
        character=CharacterConfig(name="Judge"),
        model="claude-sonnet-4-6",
    )
    health = AgentConfig(
        role="health",
        character=CharacterConfig(name="Health"),
        model="claude-sonnet-4-6",
    )
    result.specialist_runner = _SpecialistRunner()
    monkeypatch.setattr(tools, "_run_delegated_agent", result.specialist_runner)
    tools.init_tools(
        ChannelManager(), MessageBus(), result.specialists,
        agent_role_map={
            "concierge": caller,
            "judge": judge,
            "health": health,
        },
        specialist_limiter=SpecialistLimiter(max_global=4),
    )

    result.bus = MessageBus()
    result.gary = _GaryProbe()
    result.bus.register("concierge", result.gary.handle_message)
    result.bus_loop = result.bus.start_agent_loop("concierge")
    result.coordinator = VoiceDeliveryCoordinator(
        result.registry,
        None,
        lease_s=15,
        renew_s=5,
    )
    channel = VoiceChannel(
        bus=result.bus,
        default_agent="concierge",
        webhook_secret="e2e-secret",
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"concierge": caller},
        memory=AsyncMock(),
        idle_timeout=300,
        delivery_coordinator=result.coordinator,
    )
    result.channel = channel
    result.coordinator._routes = channel.routes
    app = web.Application()
    channel.register_routes(app)
    result.runner = web.AppRunner(app)
    await result.runner.setup()
    site = web.TCPSite(result.runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    result.session = aiohttp.ClientSession()
    result.api = CasaApiClient(
        result.session, "127.0.0.1", port, "e2e-secret",
    )
    directory = SatelliteDirectory()
    directory.add("dev-k", "assist_satellite.kitchen")
    directory.add("dev-o", "assist_satellite.office")
    directory.set_state("dev-k", "processing", changed_at=time.time())
    directory.set_state("dev-o", "processing", changed_at=time.time())
    services = SimpleNamespace()

    async def announce(domain, service, data, *, blocking):
        assert (domain, service, blocking) == (
            "assist_satellite", "announce", True,
        )
        result.announces.append(dict(data))

    services.async_call = announce
    hass = SimpleNamespace(services=services)
    result.directory = directory
    result.manager = BackgroundDeliveryManager(
        hass,
        result.api,
        route_id="entry-1",
        directory=directory,
        idle_stability_ms=750,
    )
    result.inbound_job_frames = []

    async def capture_job_frame(frame):
        result.inbound_job_frames.append(dict(frame))
        await result.manager.handle_frame(frame)

    await result.api.start_background(
        route_id="entry-1",
        agent_role="concierge",
        job_handler=capture_job_frame,
    )
    await _eventually(lambda: result.api.background_capable)
    await result.coordinator.start()
    try:
        yield result
    finally:
        await result.close()


async def test_busy_completion_waits_then_announces_without_reentering_gary(stack):
    pending, elapsed = await stack.submit()
    assert pending["status"] == "pending"
    assert elapsed <= 3.5

    assert await stack.second_utterance() == ["block", "done"]
    before_completion = stack.gary._pool.stats().copy()
    await stack.specialist_runner.finish()
    job = await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await stack.coordinator.sweep_once()
    await _eventually(lambda: stack.manager.attempt_count_for_test == 1)
    assert job.delivery_state is DeliveryState.READY
    assert stack.announces == []

    stack.directory.set_state(
        "dev-k", "idle", changed_at=time.time() - 1,
    )
    await _eventually(
        lambda: (
            stack.registry.get(pending["job_id"]).delivery_state
            is DeliveryState.DELIVERED
        ),
    )

    assert [call["message"] for call in stack.announces] == ["The ruling is no."]
    assert stack.gary._pool.stats() == before_completion
    assert not any(
        message.type is MessageType.NOTIFICATION
        for message in stack.bus.get_log()
    )


async def test_immediate_idle_completion_announces_without_extra_transition(stack):
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    pending, _ = await stack.submit()
    await stack.specialist_runner.finish(spoken="Immediate answer.")
    await stack.registry.wait_for_terminal(pending["job_id"])
    await stack.coordinator.sweep_once()

    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.DELIVERED,
    )
    assert [call["message"] for call in stack.announces] == ["Immediate answer."]


async def test_cancel_running_never_announces(stack):
    pending, _ = await stack.submit()
    job = stack.registry.get(pending["job_id"])
    await stack.registry.request_cancel(pending["job_id"], actor={
        "creator_peer": job.creator_peer,
        "creator_user_id": job.creator_user_id,
        "scope_id": job.scope_id,
    })
    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).execution_state
        is ExecutionState.CANCELLED,
    )
    await stack.coordinator.sweep_once()
    assert stack.announces == []


@pytest.mark.parametrize("phase", ["ready", "claimed"])
async def test_cancel_before_playback_never_announces(stack, phase):
    pending, _ = await stack.submit()
    await stack.specialist_runner.finish()
    await stack.registry.wait_for_terminal(pending["job_id"])
    if phase == "claimed":
        await stack.coordinator.sweep_once()
        await _eventually(
            lambda: (
                stack.registry.get(pending["job_id"]).delivery_state
                is DeliveryState.CLAIMED
            ),
        )
    job = stack.registry.get(pending["job_id"])
    await stack.registry.request_cancel(pending["job_id"], actor={
        "creator_peer": job.creator_peer,
        "creator_user_id": job.creator_user_id,
        "scope_id": job.scope_id,
    })
    await stack.coordinator.sweep_once()
    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.CANCELLED,
    )
    assert stack.announces == []


async def test_ws_reconnect_preserves_background_delivery(stack):
    await stack.reconnect()
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    pending, _ = await stack.submit()
    await stack.specialist_runner.finish(spoken="After reconnect.")
    await stack.registry.wait_for_terminal(pending["job_id"])
    await stack.coordinator.sweep_once()

    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.DELIVERED,
    )
    assert [call["message"] for call in stack.announces] == ["After reconnect."]


async def test_private_result_canaries_never_reach_wire_or_logs(stack, caplog):
    summary = "PRIVATE_E2E_SUMMARY_CANARY"
    answer = "PRIVATE_E2E_ANSWER_CANARY"
    citation = "PRIVATE_E2E_CITATION_CANARY"
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    pending, _ = await stack.submit()

    with caplog.at_level("DEBUG"):
        await stack.specialist_runner.finish(
            spoken=summary,
            answer=answer,
            citation=citation,
            sensitivity="private",
        )
        await stack.registry.wait_for_terminal(pending["job_id"])
        await stack.coordinator.sweep_once()
        await _eventually(
            lambda: stack.registry.get(pending["job_id"]).delivery_state
            is DeliveryState.DELIVERED,
        )

    assert [call["message"] for call in stack.announces] == [
        "Your result is ready; ask me for the details.",
    ]
    serialized_wire = json.dumps(stack.inbound_job_frames)
    for canary in (summary, answer, citation):
        assert canary not in json.dumps(pending)
        assert canary not in serialized_wire
        assert canary not in caplog.text


async def test_real_reader_writer_interleave_keeps_devices_and_frames_isolated(
    stack, monkeypatch,
):
    from custom_components.casa import delivery as integration_delivery

    monkeypatch.setattr(integration_delivery, "_LEASE_RENEW_SECONDS", 0.05)
    client_ws = stack.api._ws
    server_route = stack.channel.routes.get_connected("entry-1")
    assert client_ws is not None and server_route is not None
    server_ws = server_route.connection._ws
    send_activity = {
        "client": 0, "client_max": 0,
        "server": 0, "server_max": 0,
    }

    async def instrument(name, send, *args, **kwargs):
        send_activity[name] += 1
        send_activity[f"{name}_max"] = max(
            send_activity[f"{name}_max"], send_activity[name],
        )
        await asyncio.sleep(0)
        try:
            return await send(*args, **kwargs)
        finally:
            send_activity[name] -= 1

    client_send = client_ws.send_json
    server_send = server_ws.send_json

    async def instrument_client(*args, **kwargs):
        return await instrument("client", client_send, *args, **kwargs)

    async def instrument_server(*args, **kwargs):
        return await instrument("server", server_send, *args, **kwargs)

    monkeypatch.setattr(client_ws, "send_json", instrument_client)
    monkeypatch.setattr(server_ws, "send_json", instrument_server)

    kitchen, _ = await stack.submit(task="Kitchen ruling", device_id="dev-k")
    office, _ = await stack.submit(
        task="Office health question",
        device_id="dev-o",
        agent="health",
    )
    await stack.specialist_runner.finish(spoken="Kitchen answer.")
    await stack.specialist_runner.finish(spoken="Office answer.")
    await asyncio.gather(
        stack.registry.wait_for_terminal(kitchen["job_id"]),
        stack.registry.wait_for_terminal(office["job_id"]),
    )
    await stack.coordinator.sweep_once()
    await _eventually(lambda: all(
        stack.registry.get(job_id).delivery_state is DeliveryState.CLAIMED
        for job_id in (kitchen["job_id"], office["job_id"])
    ))
    initial_office_lease = stack.registry.get(office["job_id"]).lease_until

    utterance_one = asyncio.create_task(stack.second_utterance("utterance-a"))
    utterance_two = asyncio.create_task(stack.second_utterance("utterance-b"))
    await asyncio.sleep(0.12)
    assert stack.registry.get(office["job_id"]).lease_until > initial_office_lease

    kitchen_job = stack.registry.get(kitchen["job_id"])
    await stack.registry.request_cancel(kitchen_job.id, actor={
        "creator_peer": kitchen_job.creator_peer,
        "creator_user_id": kitchen_job.creator_user_id,
        "scope_id": kitchen_job.scope_id,
    })
    await stack.coordinator.sweep_once()
    stack.directory.set_state("dev-o", "idle", changed_at=time.time() - 1)

    frames_one, frames_two = await asyncio.gather(utterance_one, utterance_two)
    await _eventually(
        lambda: (
            stack.registry.get(office["job_id"]).delivery_state
            is DeliveryState.DELIVERED
        ),
    )
    await _eventually(
        lambda: (
            stack.registry.get(kitchen["job_id"]).delivery_state
            is DeliveryState.CANCELLED
        ),
    )

    assert frames_one == ["block", "done"]
    assert frames_two == ["block", "done"]
    assert [call["message"] for call in stack.announces] == ["Office answer."]
    assert send_activity["client_max"] == 1
    assert send_activity["server_max"] == 1
