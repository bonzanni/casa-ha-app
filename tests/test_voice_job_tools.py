"""Metadata-only voice job tools and asynchronous voice acceptance.

These tests deliberately use the real durable JobRegistry and specialist
limiter.  Only the external specialist SDK turn is controlled, so lifecycle,
authorization, ambiguity, cancellation, and permit behavior remain real.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

import agent as agent_mod
import tools
from bus import MessageBus
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, DelegateEntry
from job_registry import (
    DeliveryState,
    ExecutionState,
    JobFailure,
    JobRegistry,
    VoiceJob,
)
from specialist_limits import SpecialistLimiter
from specialist_registry import SpecialistRegistry


pytestmark = pytest.mark.unit


def _caller_cfg() -> AgentConfig:
    cfg = AgentConfig(role="concierge")
    cfg.delegates = [
        DelegateEntry(agent="judge", purpose="rules", when="rules question"),
        DelegateEntry(agent="health", purpose="health", when="health question"),
    ]
    return cfg


def _specialist_cfg(role: str, display_name: str) -> AgentConfig:
    return AgentConfig(
        role=role,
        character=CharacterConfig(name=display_name),
        model="claude-sonnet-4-6",
    )


def voice_origin(**overrides) -> dict:
    origin = {
        "role": "concierge",
        "execution_role": "concierge",
        "channel": "voice",
        "chat_id": "scope-1",
        "user_id": "user-1",
        "cid": "turn-1",
        "user_text": "Does this target?",
        "voice_transport": "ws",
        "voice_route_id": "entry-1",
        "voice_route_capabilities": frozenset({
            "background_jobs", "satellite_announce",
        }),
        "origin_device_id": "device-kitchen",
    }
    origin.update(overrides)
    return origin


def _structured_result(**overrides) -> dict:
    return {
        "status": "answered",
        "spoken_summary": "The ruling is no.",
        "answer": "No, because it does not target.",
        "clarification": "",
        "citations": ["CR 115.1"],
        "assumptions": [],
        "provenance": {},
        "sensitivity": "household",
        "delivery_ttl_s": 900,
        **overrides,
    }


def tool_payload(envelope: dict) -> dict:
    return json.loads(envelope["content"][0]["text"])


async def _call(tool, origin: dict, args: dict) -> dict:
    token = agent_mod.origin_var.set(origin)
    try:
        return await tool.handler(args)
    finally:
        agent_mod.origin_var.reset(token)


class _ControlledRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.outputs: asyncio.Queue[tools.DelegatedOutput | BaseException] = (
            asyncio.Queue()
        )

    async def __call__(
        self, cfg, task_text, context_text, resolution=None, output_format=None,
    ) -> tools.DelegatedOutput:
        self.calls.append({
            "cfg": cfg,
            "task": task_text,
            "context": context_text,
            "resolution": resolution,
            "output_format": output_format,
        })
        self.started.set()
        output = await self.outputs.get()
        if isinstance(output, BaseException):
            raise output
        return output

    async def finish(self, **overrides) -> None:
        await self.outputs.put(tools.DelegatedOutput(
            text=overrides.pop("text", "PRIVATE_RESULT_CANARY"),
            structured_output=_structured_result(**overrides),
        ))

    async def fail(self, exc: BaseException) -> None:
        await self.outputs.put(exc)


class ToolEnv:
    def __init__(
        self,
        registry: JobRegistry,
        specialist_registry: SpecialistRegistry,
        limiter: SpecialistLimiter,
        runner: _ControlledRunner,
    ) -> None:
        self.job_registry = registry
        self.specialist_registry = specialist_registry
        self.limiter = limiter
        self.runner = runner

    async def invoke_delegate(
        self, origin: dict | None = None, *, mode: str = "async",
        agent: str = "judge", task: str = "Does this target?", context: str = "",
    ) -> dict:
        return await _call(
            tools.delegate_to_agent,
            origin or voice_origin(),
            {"agent": agent, "task": task, "context": context, "mode": mode},
        )

    async def add_job(self, job_id: str, **changes) -> VoiceJob:
        sequence = len(self.job_registry.all()) + 1
        base = VoiceJob(
            id=job_id,
            parent_job_id=None,
            creating_role="concierge",
            specialist_role="judge",
            specialist_display_name="Judge",
            creator_peer="voice",
            creator_user_id="user-1",
            scope_id="scope-1",
            origin_route_id="entry-1",
            origin_device_id="device-kitchen",
            task="PRIVATE_TASK_CANARY",
            context="PRIVATE_CONTEXT_CANARY",
            created_at=time.time(),
            started_at=time.time(),
            terminal_at=None,
            expires_at=None,
            execution_state=ExecutionState.RUNNING,
            delivery_state=DeliveryState.NONE,
            result=None,
            failure=None,
            awaiting_input=False,
            continuable_until=None,
            delivery_sequence=sequence,
            delivery_attempt_id=None,
            lease_until=None,
            cancel_pending=False,
        )
        job = replace(base, **changes)
        await self.job_registry.create(job)
        return job


@pytest.fixture
async def tool_env(tmp_path, monkeypatch):
    registry = JobRegistry(tmp_path / "jobs.json", tmp_path / "delegations.json")
    await registry.load()
    specialist_registry = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=registry,
    )
    limiter = SpecialistLimiter(max_global=4)
    runner = _ControlledRunner()
    monkeypatch.setattr(tools, "_run_delegated_agent", runner)
    tools.init_tools(
        ChannelManager(), MessageBus(), specialist_registry,
        agent_role_map={
            "concierge": _caller_cfg(),
            "judge": _specialist_cfg("judge", "Judge"),
            "health": _specialist_cfg("health", "Health"),
        },
        specialist_limiter=limiter,
    )
    env = ToolEnv(registry, specialist_registry, limiter, runner)
    try:
        yield env
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_voice_async_accepts_and_returns_only_opaque_metadata(tool_env):
    result = await tool_env.invoke_delegate()
    payload = tool_payload(result)

    assert payload == {
        "status": "pending",
        "job_id": payload["job_id"],
        "specialist_display_name": "Judge",
    }
    assert "task" not in payload and "text" not in payload
    jobs = tool_env.job_registry.all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == payload["job_id"]
    assert job.execution_state is ExecutionState.RUNNING
    assert job.origin_route_id == "entry-1"
    assert job.origin_device_id == "device-kitchen"
    assert job.task == "Does this target?"
    assert tool_env.limiter.in_flight == 1


@pytest.mark.asyncio
async def test_voice_async_failure_persists_only_a_safe_ready_envelope(tool_env):
    accepted = tool_payload(await tool_env.invoke_delegate())
    job_id = accepted["job_id"]
    await tool_env.runner.fail(RuntimeError("PRIVATE_FAILURE_CANARY"))

    for _ in range(100):
        job = tool_env.job_registry.get(job_id)
        if job is not None and job.execution_state is ExecutionState.FAILED:
            break
        await asyncio.sleep(0)

    job = tool_env.job_registry.get(job_id)
    assert job.execution_state is ExecutionState.FAILED
    assert job.delivery_state is DeliveryState.READY
    assert job.result is None
    assert job.failure.kind == "unknown"
    assert job.failure.message == "Specialist could not complete the voice job."
    assert "PRIVATE_FAILURE_CANARY" not in json.dumps({
        "kind": job.failure.kind,
        "message": job.failure.message,
    })


@pytest.mark.asyncio
async def test_registry_owns_permit_until_terminal_persistence_and_close_waits(
    tool_env, monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    real_finish = tool_env.job_registry.finish_voice_result

    async def blocked_finish(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_finish(*args, **kwargs)

    monkeypatch.setattr(
        tool_env.job_registry, "finish_voice_result", blocked_finish,
    )
    accepted = tool_payload(await tool_env.invoke_delegate())
    await tool_env.runner.finish()
    await entered.wait()

    close_task = asyncio.create_task(tool_env.job_registry.close())
    await asyncio.sleep(0)
    assert close_task.done() is False
    assert tool_env.limiter.in_flight == 1

    release.set()
    await close_task
    job = tool_env.job_registry.get(accepted["job_id"])
    assert job.execution_state is ExecutionState.SUCCEEDED
    assert job.delivery_state is DeliveryState.READY
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_terminal_write_failure_uses_safe_fallback_without_private_log(
    tool_env, monkeypatch, caplog,
):
    async def fail_finish(*_args, **_kwargs):
        raise OSError("PRIVATE_PERSISTENCE_CANARY")

    monkeypatch.setattr(
        tool_env.job_registry, "finish_voice_result", fail_finish,
    )
    accepted = tool_payload(await tool_env.invoke_delegate())
    await tool_env.runner.finish()
    for _ in range(100):
        job = tool_env.job_registry.get(accepted["job_id"])
        if job is not None and job.execution_state is ExecutionState.FAILED:
            break
        await asyncio.sleep(0)

    job = tool_env.job_registry.get(accepted["job_id"])
    assert job.execution_state is ExecutionState.FAILED
    assert job.delivery_state is DeliveryState.READY
    assert job.failure == JobFailure(
        "persistence_failed", "Specialist result could not be saved.",
    )
    assert "PRIVATE_PERSISTENCE_CANARY" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    voice_origin(voice_route_id=None),
    voice_origin(voice_route_capabilities=frozenset()),
    voice_origin(voice_route_capabilities=frozenset({"background_jobs"})),
    voice_origin(voice_transport="sse"),
])
async def test_voice_async_without_capable_route_fails_before_side_effect(
    origin, tool_env,
):
    payload = tool_payload(await tool_env.invoke_delegate(origin))
    assert payload["kind"] == "background_delivery_unavailable"
    assert tool_env.job_registry.all() == []
    assert tool_env.runner.calls == []
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_status_never_returns_result_task_or_context_text(tool_env):
    canary = "PRIVATE_RESULT_CANARY"
    await tool_env.add_job(
        "job-1",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(answer=canary)),
    )

    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-1"},
    ))
    assert payload == {
        "status": "succeeded",
        "job_id": "job-1",
        "specialist_display_name": "Judge",
        "awaiting_input": False,
        "delivery_status": "ready",
    }
    serialized = json.dumps(payload)
    assert canary not in serialized
    assert "PRIVATE_TASK_CANARY" not in serialized
    assert "PRIVATE_CONTEXT_CANARY" not in serialized


@pytest.mark.asyncio
async def test_explicit_status_can_inspect_an_owned_terminal_job(tool_env):
    await tool_env.add_job(
        "job-delivered",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
    )

    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-delivered"},
    ))
    assert payload == {
        "status": "succeeded",
        "job_id": "job-delivered",
        "specialist_display_name": "Judge",
        "awaiting_input": False,
        "delivery_status": "delivered",
    }


@pytest.mark.asyncio
async def test_unauthorized_explicit_status_is_indistinguishable_from_missing(tool_env):
    await tool_env.add_job(
        "job-private", creator_user_id="other-user",
        result="PRIVATE_RESULT_CANARY",
    )
    denied = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-private"},
    ))
    missing = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "does-not-exist"},
    ))
    assert denied == missing
    assert denied["kind"] == "job_not_found"
    assert "PRIVATE" not in json.dumps(denied)


@pytest.mark.asyncio
async def test_anonymous_job_requires_an_exact_anonymous_actor(tool_env):
    await tool_env.add_job("job-anonymous", creator_user_id=None)
    denied = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-anonymous"},
    ))
    missing = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "does-not-exist"},
    ))
    assert denied == missing

    allowed = tool_payload(await _call(
        tools.voice_job_status,
        voice_origin(user_id=None),
        {"job_id": "job-anonymous"},
    ))
    assert allowed["job_id"] == "job-anonymous"


@pytest.mark.asyncio
async def test_omitted_status_id_selects_the_only_authorized_job(tool_env):
    await tool_env.add_job("job-mine")
    await tool_env.add_job("job-not-mine", creator_user_id="other-user")

    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {},
    ))
    assert payload == {
        "status": "running",
        "job_id": "job-mine",
        "specialist_display_name": "Judge",
        "awaiting_input": False,
        "delivery_status": "none",
    }


@pytest.mark.asyncio
async def test_omitted_cancel_id_requires_exactly_one_match(tool_env):
    await tool_env.add_job("job-1")
    await tool_env.add_job(
        "job-2", specialist_role="health", specialist_display_name="Health",
    )

    payload = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {},
    ))
    assert payload["kind"] == "ambiguous_job"
    assert payload["choices"] == [
        {"job_id": "job-1", "specialist_display_name": "Judge"},
        {"job_id": "job-2", "specialist_display_name": "Health"},
    ]
    assert tool_env.job_registry.get("job-1").cancel_pending is False
    assert tool_env.job_registry.get("job-2").cancel_pending is False


@pytest.mark.asyncio
async def test_unauthorized_cancel_is_indistinguishable_from_missing(tool_env):
    await tool_env.add_job("job-private", creator_user_id="other-user")
    denied = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {"job_id": "job-private"},
    ))
    missing = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {"job_id": "does-not-exist"},
    ))
    assert denied == missing
    assert denied["kind"] == "job_not_found"
    assert tool_env.job_registry.get("job-private").cancel_pending is False


@pytest.mark.asyncio
async def test_cancel_authorizes_with_trusted_actor_and_hides_private_fields(tool_env):
    await tool_env.add_job("job-1")
    payload = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {"job_id": "job-1"},
    ))
    assert payload == {
        "status": "stopping",
        "job_id": "job-1",
        "specialist_display_name": "Judge",
    }
    assert "PRIVATE" not in json.dumps(payload)
    assert tool_env.job_registry.get("job-1").cancel_pending is True


@pytest.mark.asyncio
async def test_cancel_completion_race_has_one_honest_terminal_winner(
    tool_env, monkeypatch,
):
    monkeypatch.setattr(JobRegistry, "CANCEL_GRACE_SECONDS", 0.0)
    accepted = tool_payload(await tool_env.invoke_delegate())
    job_id = accepted["job_id"]
    await tool_env.runner.finish()
    cancel_task = asyncio.create_task(_call(
        tools.cancel_voice_job, voice_origin(), {"job_id": job_id},
    ))
    payload = tool_payload(await cancel_task)
    for _ in range(100):
        job = tool_env.job_registry.get(job_id)
        if job is not None and job.execution_state in {
            ExecutionState.SUCCEEDED, ExecutionState.CANCELLED,
        }:
            break
        await asyncio.sleep(0)
    job = tool_env.job_registry.get(job_id)
    assert payload["status"] in {"stopping", "cancelled", "too_late"}
    assert job.execution_state in {ExecutionState.SUCCEEDED, ExecutionState.CANCELLED}
    assert not (job.execution_state is ExecutionState.SUCCEEDED and job.cancel_pending)
    for _ in range(100):
        if tool_env.limiter.in_flight == 0:
            break
        await asyncio.sleep(0)
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_continue_job_copies_private_backend_context_without_returning_it(tool_env):
    canary = "PRIVATE_PRIOR_RESULT_CANARY"
    parent = await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which card do you mean?",
            clarification="Which card do you mean?",
            answer=canary,
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )

    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(origin_device_id="device-office", voice_route_id="entry-2"),
        {"input": "I mean Black Lotus", "job_id": ""},
    ))
    assert payload == {
        "status": "pending",
        "job_id": payload["job_id"],
        "specialist_display_name": "Judge",
    }
    assert canary not in json.dumps(payload)
    child = tool_env.job_registry.get(payload["job_id"])
    consumed_parent = tool_env.job_registry.get(parent.id)
    assert consumed_parent.awaiting_input is False
    assert consumed_parent.continuable_until is None
    assert child.parent_job_id == parent.id
    assert child.origin_route_id == "entry-2"
    assert child.origin_device_id == "device-office"
    assert child.task == "I mean Black Lotus"
    assert canary in child.context
    await tool_env.runner.started.wait()
    assert canary in tool_env.runner.calls[-1]["context"]


@pytest.mark.asyncio
async def test_continuation_parent_can_be_consumed_only_once(tool_env):
    await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which one?",
            clarification="Which one?",
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    first = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "first", "job_id": "job-parent"},
    ))
    assert first["status"] == "pending"
    await tool_env.runner.finish()
    for _ in range(100):
        if tool_env.limiter.in_flight == 0:
            break
        await asyncio.sleep(0)

    second = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "second", "job_id": "job-parent"},
    ))
    assert second["kind"] == "job_not_continuable"
    assert len(tool_env.job_registry.all()) == 2


@pytest.mark.asyncio
async def test_omitted_continue_id_is_ambiguous_and_creates_no_child(tool_env):
    for job_id, role, display in (
        ("job-1", "judge", "Judge"),
        ("job-2", "health", "Health"),
    ):
        await tool_env.add_job(
            job_id,
            specialist_role=role,
            specialist_display_name=display,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=time.time(),
            expires_at=time.time() + 900,
            result=json.dumps(_structured_result()),
            awaiting_input=True,
            continuable_until=time.time() + 900,
        )

    payload = tool_payload(await _call(
        tools.continue_voice_job, voice_origin(), {"input": "more", "job_id": ""},
    ))
    assert payload["kind"] == "ambiguous_job"
    assert payload["choices"] == [
        {"job_id": "job-1", "specialist_display_name": "Judge"},
        {"job_id": "job-2", "specialist_display_name": "Health"},
    ]
    assert [job.id for job in tool_env.job_registry.all()] == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_unauthorized_continue_is_indistinguishable_from_missing(tool_env):
    await tool_env.add_job(
        "job-private",
        creator_user_id="other-user",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    denied = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "more", "job_id": "job-private"},
    ))
    missing = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "more", "job_id": "does-not-exist"},
    ))
    assert denied == missing
    assert denied["kind"] == "job_not_found"
    assert len(tool_env.job_registry.all()) == 1


@pytest.mark.asyncio
async def test_expired_continuation_is_rejected_without_child(tool_env):
    await tool_env.add_job(
        "job-expired",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time() - 120,
        expires_at=time.time() - 60,
        result=json.dumps(_structured_result()),
        awaiting_input=True,
        continuable_until=time.time() - 60,
    )
    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "too late", "job_id": "job-expired"},
    ))
    assert payload["kind"] == "job_not_continuable"
    assert len(tool_env.job_registry.all()) == 1


@pytest.mark.asyncio
async def test_continue_without_current_capable_route_creates_no_child(tool_env):
    await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(voice_transport="sse", voice_route_id=None),
        {"input": "more", "job_id": "job-parent"},
    ))
    assert payload["kind"] == "background_delivery_unavailable"
    assert len(tool_env.job_registry.all()) == 1


def test_voice_job_tools_are_registered_on_both_framework_surfaces():
    names = {candidate.name for candidate in tools.CASA_TOOLS}
    assert {
        "voice_job_status", "cancel_voice_job", "continue_voice_job",
    } <= names
    selected = {
        candidate.name for candidate in tools.select_casa_tools(frozenset({
            "mcp__casa-framework__voice_job_status",
            "mcp__casa-framework__cancel_voice_job",
            "mcp__casa-framework__continue_voice_job",
        }))
    }
    assert selected == {
        "voice_job_status", "cancel_voice_job", "continue_voice_job",
    }
