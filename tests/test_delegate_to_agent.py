"""Tests for the delegate_to_agent framework tool (Phase 3.1)."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, SessionConfig,
    ToolsConfig,
)
from plugin_registry import ResolutionResult
from specialist_registry import (
    DelegationComplete,
    DelegationRecord,
    SpecialistRegistry,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.asyncio


def _seed_specialist_dir(
    base: Path, role: str = "finance", *, enabled: bool = True,
) -> Path:
    """Write a valid specialist directory under *base*. Returns the dir path."""
    d = base / role
    d.mkdir(parents=True)
    (d / "character.yaml").write_text(textwrap.dedent(f"""\
        schema_version: 1
        name: {role.capitalize()}
        role: {role}
        archetype: exec
        card: |
          x
        prompt: |
          x
    """), encoding="utf-8")
    (d / "voice.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (d / "response_shape.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (d / "runtime.yaml").write_text(textwrap.dedent(f"""\
        schema_version: 1
        model: sonnet
        enabled: {str(enabled).lower()}
        tools:
          allowed: [Read]
        memory:
          token_budget: 0
        session:
          strategy: ephemeral
    """), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _specialist_cfg(role: str = "finance", enabled: bool = True) -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, 
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are " + role,
        character=CharacterConfig(name=role.capitalize()),
        enabled=enabled,
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=0),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


class _FakeSpecialistClient:
    """Minimal ClaudeSDKClient substitute for specialist turns.

    ``response_text`` is the text yielded by an AssistantMessage block.
    ``delay_s`` sleeps inside ``receive_response`` so timeout tests can
    drive the 60s degradation path without actually waiting 60s.
    """

    response_text: str = "finance reply"
    structured_output: Any = None
    captured_options: Any = None
    delay_s: float = 0.0
    raise_in_receive: Exception | None = None

    @classmethod
    def reset(
        cls, response="finance reply", delay=0.0, raise_exc=None,
        structured_output=None,
    ):
        cls.response_text = response
        cls.structured_output = structured_output
        cls.captured_options = None
        cls.delay_s = delay
        cls.raise_in_receive = raise_exc

    def __init__(self, options):
        self.options = options
        type(self).captured_options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        self._text = text

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, TextBlock, SystemMessage,
        )
        if _FakeSpecialistClient.delay_s > 0:
            await asyncio.sleep(_FakeSpecialistClient.delay_s)
        if _FakeSpecialistClient.raise_in_receive is not None:
            raise _FakeSpecialistClient.raise_in_receive

        # SDK shape has drifted: fields like AssistantMessage.model and
        # ResultMessage's positional args may be absent on older SDKs.
        # Mirror the `_mk_*` helpers in test_agent_process.py — try the
        # kwargs form, fall back to __new__ + attribute assignment.
        try:
            block = TextBlock(text=_FakeSpecialistClient.response_text)
        except TypeError:
            block = TextBlock(_FakeSpecialistClient.response_text)  # type: ignore[call-arg]
        try:
            sys_msg = SystemMessage(
                subtype="init", data={"session_id": "exec-sid"},
            )
        except TypeError:
            sys_msg = SystemMessage.__new__(SystemMessage)
            sys_msg.subtype = "init"  # type: ignore[attr-defined]
            sys_msg.data = {"session_id": "exec-sid"}  # type: ignore[attr-defined]
        yield sys_msg
        try:
            asst = AssistantMessage(content=[block])
        except TypeError:
            asst = AssistantMessage.__new__(AssistantMessage)
            asst.content = [block]  # type: ignore[attr-defined]
        yield asst
        try:
            result = ResultMessage(session_id="exec-sid")
        except TypeError:
            result = ResultMessage.__new__(ResultMessage)
            result.session_id = "exec-sid"  # type: ignore[attr-defined]
        object.__setattr__(
            result, "structured_output", _FakeSpecialistClient.structured_output,
        )
        yield result


async def _with_origin(coro, origin: dict[str, Any]):
    """Run *coro* with origin_var pre-set, emulating an in-turn call."""
    import agent as agent_mod
    token = agent_mod.origin_var.set(origin)
    try:
        return await coro
    finally:
        agent_mod.origin_var.reset(token)


def _origin(role="assistant", channel="telegram", chat_id="x"):
    return {
        "role": role,
        "channel": channel,
        "chat_id": chat_id,
        "cid": "c1",
        "user_text": "please do X",
    }


def _caller_cfg(role: str = "assistant", delegates: tuple[str, ...] = ("finance",)) -> AgentConfig:
    """Minimal caller AgentConfig declaring *delegates* (spec A1 ACL fixture).

    The delegation ACL denies any target the caller's `origin["role"]`
    doesn't declare, so every fixture that drives `delegate_to_agent`
    must seed the caller into `agent_role_map` with the target declared.
    """
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    return cfg


# ---------------------------------------------------------------------------
# TestUnknownAgent / TestDisabledAgent
# ---------------------------------------------------------------------------


class TestUnknownAgent:
    async def test_returns_error_content(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("ghost",))})

        result = await _with_origin(
            delegate_to_agent.handler({
                "agent": "ghost", "task": "x", "context": "", "mode": "sync",
            }),
            _origin(),
        )
        assert "content" in result
        text = result["content"][0]["text"]
        payload = json.loads(text)
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_agent"


class TestDisabledAgent:
    async def test_returns_unknown_agent_error(self, tmp_path):
        """Disabled specialists are filtered at load-time — get() returns None,
        the tool cannot distinguish them from truly unknown names. Both
        paths collapse to kind=unknown_agent."""
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=False)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

        result = await _with_origin(
            delegate_to_agent.handler({
                "agent": "finance", "task": "x", "context": "", "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_agent"


# ---------------------------------------------------------------------------
# TestSyncOk / TestSyncError
# ---------------------------------------------------------------------------


class TestSyncOk:
    async def test_returns_specialist_text(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

        _FakeSpecialistClient.reset(response="invoice drafted", delay=0)
        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "draft invoice",
                    "context": "lesina march",
                    "mode": "sync",
                }),
                _origin(),
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "ok"
        assert payload["agent"] == "finance"
        assert payload["text"] == "invoice drafted"
        assert "delegation_id" in payload
        assert payload["elapsed_s"] >= 0
        assert _FakeSpecialistClient.captured_options.output_format is None
        # Record was registered then cleaned up.
        assert not reg.has_delegation(payload["delegation_id"])


class TestSyncError:
    async def test_specialist_raises_is_reported_as_error(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

        _FakeSpecialistClient.reset(raise_exc=RuntimeError("boom"))
        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "x", "context": "",
                    "mode": "sync",
                }),
                _origin(),
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert "delegation_id" in payload
        assert "kind" in payload
        # Record was cleaned up.
        assert not reg.has_delegation(payload["delegation_id"])


# ---------------------------------------------------------------------------
# TestVoiceStructuredResult
# ---------------------------------------------------------------------------


class TestVoiceStructuredResult:
    @staticmethod
    def _structured_result(**overrides):
        return {
            "status": "answered",
            "spoken_summary": "The answer is 42.",
            "answer": "42",
            "clarification": "",
            "citations": [],
            "assumptions": [],
            "provenance": {},
            "sensitivity": "household",
            "delivery_ttl_s": 900,
            **overrides,
        }

    async def test_runner_captures_text_and_structured_output(
        self, tmp_path, monkeypatch,
    ):
        import tools
        from voice_job_result import VOICE_JOB_OUTPUT_FORMAT

        reg = SpecialistRegistry(
            str(tmp_path / "ex"), tombstone_path=str(tmp_path / "del.json"),
        )
        init_map = {"assistant": _caller_cfg(delegates=("finance",))}
        tools.init_tools(ChannelManager(), MessageBus(), reg, agent_role_map=init_map)
        structured = self._structured_result()
        _FakeSpecialistClient.reset(response="legacy text", structured_output=structured)
        monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)

        output = await _with_origin(
            tools._run_delegated_agent(
                _specialist_cfg(), "question", "", resolution=None,
                output_format=VOICE_JOB_OUTPUT_FORMAT,
            ),
            _origin(),
        )

        assert output == tools.DelegatedOutput(
            text="legacy text", structured_output=structured,
        )
        assert _FakeSpecialistClient.captured_options.output_format is VOICE_JOB_OUTPUT_FORMAT

    async def test_voice_runner_suppresses_sdk_protocol_payload_logs(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools
        from voice_job_result import VOICE_JOB_OUTPUT_FORMAT

        private_canary = "PRIVATE-SDK-PROTOCOL-CANARY-f618"

        class ProtocolLoggingClient(_FakeSpecialistClient):
            async def receive_response(self):
                async def _sdk_reader_log():
                    logging.getLogger(
                        "claude_agent_sdk._internal.query"
                    ).error("Fatal error in message reader: %s", private_canary)
                    logging.getLogger(
                        "claude_agent_sdk._internal.transport.subprocess_cli"
                    ).debug("Skipping CLI stdout: %s", private_canary)

                await asyncio.create_task(_sdk_reader_log())
                async for message in super().receive_response():
                    yield message

        reg = SpecialistRegistry(
            str(tmp_path / "ex"), tombstone_path=str(tmp_path / "del.json"),
        )
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={"assistant": _caller_cfg(delegates=("finance",))},
        )
        structured = self._structured_result()
        _FakeSpecialistClient.reset(
            response="safe legacy text", structured_output=structured,
        )
        monkeypatch.setattr(tools, "ClaudeSDKClient", ProtocolLoggingClient)

        with caplog.at_level(logging.DEBUG):
            output = await _with_origin(
                tools._run_delegated_agent(
                    _specialist_cfg(), "question", "", resolution=None,
                    output_format=VOICE_JOB_OUTPUT_FORMAT,
                ),
                _origin(channel="voice"),
            )

        assert output.structured_output == structured
        assert private_canary not in caplog.text

    async def test_private_voice_result_is_resolved_before_tool_envelope(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools
        from job_registry import ExecutionState
        from voice_job_result import VOICE_JOB_OUTPUT_FORMAT

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tmp_path / "del.json"),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        private_canary = "PRIVATE-VOICE-CANARY-7e6b"
        structured = self._structured_result(
            answer=private_canary,
            spoken_summary=private_canary,
            sensitivity="private",
        )
        _FakeSpecialistClient.reset(
            response=private_canary, structured_output=structured,
        )
        monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)
        origin = _origin(channel="voice")
        origin["voice_deadline"] = asyncio.get_running_loop().time() + 30.0

        with caplog.at_level(logging.DEBUG):
            envelope = await _with_origin(
                tools.delegate_to_agent.handler({
                    "agent": "finance", "task": "private question",
                    "context": "", "mode": "sync",
                }),
                origin,
            )

        payload = json.loads(envelope["content"][0]["text"])
        assert payload["status"] == "ok"
        assert payload["text"] == "Your result is ready; ask me for the details."
        assert private_canary not in json.dumps(envelope)
        assert private_canary not in caplog.text
        assert _FakeSpecialistClient.captured_options.output_format is VOICE_JOB_OUTPUT_FORMAT
        job = reg.job_registry.get(payload["delegation_id"])
        assert job is not None
        assert job.execution_state is ExecutionState.SUCCEEDED
        assert private_canary in (job.result or "")
        assert job.terminal_at is not None
        assert job.expires_at == pytest.approx(job.terminal_at + 900)
        assert job.awaiting_input is False
        assert job.continuable_until is None

        stderr_canary = "PRIVATE-STDERR-CANARY-310b"
        stderr_callback = _FakeSpecialistClient.captured_options.stderr
        assert callable(stderr_callback)
        stderr_callback(stderr_canary)
        assert stderr_canary not in caplog.text

    async def test_voice_clarification_persists_continuation_and_speaks_question(
        self, tmp_path, monkeypatch,
    ):
        import tools
        from job_registry import JobRegistry

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        tombstone_path = tmp_path / "del.json"
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tombstone_path),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        summary = "I need one more detail."
        question = "Which card do you mean?"
        structured = self._structured_result(
            status="needs_clarification",
            answer="",
            spoken_summary=summary,
            clarification=question,
            delivery_ttl_s=600,
        )
        _FakeSpecialistClient.reset(
            response="raw specialist text", structured_output=structured,
        )
        monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)
        origin = _origin(channel="voice")
        origin["voice_deadline"] = asyncio.get_running_loop().time() + 30.0

        envelope = await _with_origin(
            tools.delegate_to_agent.handler({
                "agent": "finance", "task": "ambiguous question",
                "context": "", "mode": "sync",
            }),
            origin,
        )

        payload = json.loads(envelope["content"][0]["text"])
        assert payload["text"] == question
        job = reg.job_registry.get(payload["delegation_id"])
        assert job is not None
        assert job.awaiting_input is True
        assert job.terminal_at is not None
        assert job.expires_at == pytest.approx(job.terminal_at + 600)
        assert job.continuable_until == job.expires_at

        reloaded = JobRegistry(tmp_path / "jobs.json", tombstone_path)
        await reloaded.load()
        assert reloaded.get(job.id) == job

    async def test_private_voice_clarification_withholds_question(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tmp_path / "del.json"),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        private_canary = "PRIVATE-CLARIFICATION-CANARY-391b"
        structured = self._structured_result(
            status="needs_clarification",
            answer="",
            spoken_summary="I need a private detail.",
            clarification=f"Which account contains {private_canary}?",
            sensitivity="private",
            delivery_ttl_s=600,
        )
        _FakeSpecialistClient.reset(
            response=private_canary, structured_output=structured,
        )
        monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)
        origin = _origin(channel="voice")
        origin["voice_deadline"] = asyncio.get_running_loop().time() + 30.0

        with caplog.at_level(logging.DEBUG):
            envelope = await _with_origin(
                tools.delegate_to_agent.handler({
                    "agent": "finance", "task": "ambiguous private question",
                    "context": "", "mode": "sync",
                }),
                origin,
            )

        payload = json.loads(envelope["content"][0]["text"])
        assert payload["text"] == "Your result is ready; ask me for the details."
        assert private_canary not in json.dumps(envelope)
        assert private_canary not in caplog.text
        job = reg.job_registry.get(payload["delegation_id"])
        assert job is not None and job.awaiting_input is True
        assert private_canary in (job.result or "")

    async def test_deep_provenance_fails_safely_end_to_end(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools
        from job_registry import ExecutionState

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tmp_path / "del.json"),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        private_canary = "PRIVATE-DEPTH-1000-CANARY-f120"
        provenance: Any = {private_canary: private_canary}
        for _ in range(1000):
            provenance = {"layer": provenance}
        structured = self._structured_result(provenance=provenance)
        _FakeSpecialistClient.reset(
            response=private_canary, structured_output=structured,
        )
        monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)
        origin = _origin(channel="voice")
        origin["voice_deadline"] = asyncio.get_running_loop().time() + 30.0

        with caplog.at_level(logging.DEBUG):
            envelope = await _with_origin(
                tools.delegate_to_agent.handler({
                    "agent": "finance", "task": "deep private result",
                    "context": "", "mode": "sync",
                }),
                origin,
            )

        payload = json.loads(envelope["content"][0]["text"])
        assert payload["kind"] == "invalid_specialist_result"
        assert private_canary not in json.dumps(envelope)
        assert private_canary not in caplog.text
        job = reg.job_registry.get(payload["delegation_id"])
        assert job is not None
        assert job.execution_state is ExecutionState.FAILED
        assert job.failure is not None
        assert private_canary not in repr(job.failure)

    async def test_voice_deadline_contains_cancel_resistant_private_exception(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools
        from job_registry import ExecutionState, JobRegistry

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        tombstone_path = tmp_path / "del.json"
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tombstone_path),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        private_canary = "PRIVATE-VOICE-DEADLINE-INNER-CANARY-18bd"
        started = asyncio.Event()

        async def _raise_after_cancel(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError(private_canary)

        monkeypatch.setattr(tools, "_run_delegated_agent", _raise_after_cancel)
        monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 0.5)
        monkeypatch.setattr(tools, "_VOICE_TEARDOWN_BOUND_S", 0.5)
        monkeypatch.setattr(tools, "_SYNC_WAIT_TIMEOUT_S", 0.03)

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop_contexts: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: loop_contexts.append(context))
        try:
            origin = _origin(channel="voice")
            origin["voice_deadline"] = loop.time() + 30.0
            with caplog.at_level(logging.DEBUG):
                envelope = await _with_origin(
                    tools.delegate_to_agent.handler({
                        "agent": "finance", "task": "private deadline",
                        "context": "", "mode": "sync",
                    }),
                    origin,
                )
                assert started.is_set()
                for _ in range(3):
                    gc.collect()
                    await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        payload = json.loads(envelope["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded"
        job = reg.job_registry.get(payload["delegation_id"])
        assert job is not None
        assert job.execution_state is ExecutionState.CANCELLED
        assert job.failure is not None

        reloaded = JobRegistry(tmp_path / "jobs.json", tombstone_path)
        await reloaded.load()
        assert reloaded.get(job.id) == job

        surfaces = (
            repr(loop_contexts) + caplog.text + json.dumps(envelope)
            + repr(job.failure)
        )
        assert private_canary not in surfaces

    async def test_caller_cancel_contains_private_inner_exception(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools
        from job_registry import ExecutionState

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tmp_path / "del.json"),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        private_canary = "PRIVATE-CALLER-CANCEL-INNER-CANARY-fc02"
        started = asyncio.Event()

        async def _raise_after_cancel(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError(private_canary)

        monkeypatch.setattr(tools, "_run_delegated_agent", _raise_after_cancel)
        monkeypatch.setattr(tools, "_CEILING_TEARDOWN_BOUND_S", 0.5)

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop_contexts: list[dict] = []
        envelopes: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: loop_contexts.append(context))
        try:
            origin = _origin(channel="voice")
            origin["voice_deadline"] = loop.time() + 30.0

            async def _invoke() -> None:
                envelope = await _with_origin(
                    tools.delegate_to_agent.handler({
                        "agent": "finance", "task": "private caller cancel",
                        "context": "", "mode": "sync",
                    }),
                    origin,
                )
                envelopes.append(envelope)

            with caplog.at_level(logging.DEBUG):
                caller = asyncio.create_task(_invoke())
                await started.wait()
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                for _ in range(3):
                    gc.collect()
                    await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert envelopes == []
        jobs = reg.job_registry.all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.execution_state is ExecutionState.CANCELLED
        assert job.failure is not None
        surfaces = repr(loop_contexts) + caplog.text + repr(job.failure)
        assert private_canary not in surfaces

    async def test_invalid_voice_result_persists_safe_failure(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools
        from job_registry import ExecutionState

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tmp_path / "del.json"),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        private_canary = "PRIVATE-INVALID-CANARY-e234"
        invalid = self._structured_result(
            answer=private_canary,
            spoken_summary="",
            sensitivity="private",
        )
        _FakeSpecialistClient.reset(
            response=private_canary, structured_output=invalid,
        )
        monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)
        origin = _origin(channel="voice")
        origin["voice_deadline"] = asyncio.get_running_loop().time() + 30.0

        with caplog.at_level(logging.DEBUG):
            envelope = await _with_origin(
                tools.delegate_to_agent.handler({
                    "agent": "finance", "task": "private question",
                    "context": "", "mode": "sync",
                }),
                origin,
            )

        payload = json.loads(envelope["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "invalid_specialist_result"
        assert private_canary not in json.dumps(envelope)
        assert private_canary not in caplog.text
        job = reg.job_registry.get(payload["delegation_id"])
        assert job is not None
        assert job.execution_state is ExecutionState.FAILED
        assert job.failure is not None
        assert job.failure.kind == "invalid_specialist_result"
        assert job.failure.message == "Specialist returned an invalid structured result."
        assert private_canary not in repr(job.failure)

    async def test_voice_runner_exception_is_private_everywhere(
        self, tmp_path, monkeypatch, caplog,
    ):
        import tools
        from job_registry import ExecutionState

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(
            str(specialists), tombstone_path=str(tmp_path / "del.json"),
        )
        reg.load()
        tools.init_tools(
            ChannelManager(), MessageBus(), reg,
            agent_role_map={
                "assistant": _caller_cfg(delegates=("finance",)),
                "finance": reg.get("finance"),
            },
        )
        private_canary = "PRIVATE-VOICE-EXCEPTION-CANARY-3d5d"
        _FakeSpecialistClient.reset(raise_exc=RuntimeError(private_canary))
        monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeSpecialistClient)
        origin = _origin(channel="voice")
        origin["voice_deadline"] = asyncio.get_running_loop().time() + 30.0

        with caplog.at_level(logging.DEBUG):
            envelope = await _with_origin(
                tools.delegate_to_agent.handler({
                    "agent": "finance", "task": "private question",
                    "context": "", "mode": "sync",
                }),
                origin,
            )

        payload = json.loads(envelope["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["message"] == "Specialist could not complete the voice job."
        assert private_canary not in json.dumps(envelope)
        assert private_canary not in caplog.text
        job = reg.job_registry.get(payload["delegation_id"])
        assert job is not None
        assert job.execution_state is ExecutionState.FAILED
        assert job.failure is not None
        assert job.failure.message == "Specialist could not complete the voice job."
        assert private_canary not in repr(job.failure)

    async def test_late_voice_exception_log_is_metadata_only(self, caplog):
        import tools

        private_canary = "PRIVATE-LATE-CANCEL-CANARY-8e18"

        async def _raise():
            raise RuntimeError(private_canary)

        task = asyncio.create_task(_raise())
        await asyncio.wait({task})
        with caplog.at_level(logging.DEBUG):
            tools._retrieve_late_task_exception(task)
        assert private_canary not in caplog.text

    async def test_structured_private_detail_never_enters_delegation_complete(
        self, tmp_path,
    ):
        import tools

        private_canary = "PRIVATE-COMPLETE-CANARY-4c0a"
        reg = SpecialistRegistry(
            str(tmp_path / "ex"), tombstone_path=str(tmp_path / "del.json"),
        )
        bus = MessageBus()
        bus.register("assistant", None)
        tools.init_tools(ChannelManager(), bus, reg)
        record = DelegationRecord(
            id="delegation-canary", agent="finance",
            started_at=asyncio.get_running_loop().time(),
            origin=_origin(),
        )
        await reg.register_delegation(record)

        async def _done():
            return tools.DelegatedOutput(
                text="safe legacy text",
                structured_output={"spoken_summary": private_canary},
            )

        task = asyncio.create_task(_done())
        tools._attach_completion_callback(task, record)
        await task
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        _priority, _sequence, message = await bus.queues["assistant"].get()
        assert isinstance(message.content, DelegationComplete)
        assert message.content.text == "safe legacy text"
        assert private_canary not in repr(message.content)


# ---------------------------------------------------------------------------
# TestOriginMissing
# ---------------------------------------------------------------------------


class TestOriginMissing:
    async def test_no_origin_returns_error(self, tmp_path):
        """Called outside a turn (origin_var unset) — shouldn't happen
        in prod but must not crash. With the A1 ACL enforced first, a
        missing origin means an empty/unknown caller role, which the ACL
        denies as delegation_not_declared (the caller-identity check
        subsumes the old no_origin branch)."""
        from tools import delegate_to_agent, init_tools

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        # NOTE: not wrapped in _with_origin — origin_var stays None.
        result = await delegate_to_agent.handler({
            "agent": "finance", "task": "x", "context": "", "mode": "sync",
        })
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "delegation_not_declared"


# ---------------------------------------------------------------------------
# TestTimeoutDegrades
# ---------------------------------------------------------------------------


class TestTimeoutDegrades:
    async def test_sync_over_timeout_returns_pending(
        self, tmp_path, monkeypatch,
    ):
        """A sync call whose specialist exceeds the 60s wait returns a
        pending marker. Here we monkeypatch the wait ceiling to 50ms
        so we don't actually wait 60s."""
        from tools import delegate_to_agent, init_tools
        import tools as tools_mod

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)  # queue to receive the late NOTIFICATION
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

        # Make the specialist body take "longer" than we wait.
        _FakeSpecialistClient.reset(response="eventual", delay=0.2)
        monkeypatch.setattr(tools_mod, "_SYNC_WAIT_TIMEOUT_S", 0.05)

        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "slow task",
                    "context": "", "mode": "sync",
                }),
                _origin(),
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["agent"] == "finance"
        assert "delegation_id" in payload

        # Let the background task finish so we don't leak it.
        await asyncio.sleep(0.3)

    async def test_degraded_path_eventually_posts_notification(
        self, tmp_path, monkeypatch,
    ):
        """After the pending return, the completion callback should post
        a NOTIFICATION to the delegator's bus queue."""
        from tools import delegate_to_agent, init_tools
        import tools as tools_mod

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)  # Ellen's queue
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

        _FakeSpecialistClient.reset(response="late result", delay=0.1)
        monkeypatch.setattr(tools_mod, "_SYNC_WAIT_TIMEOUT_S", 0.02)

        # Keep both patches active across the NOTIFICATION poll: post-fix the
        # builder runs via asyncio.to_thread, so the handler returns `pending`
        # before the background _run_delegated_agent task constructs the
        # client. If the patch reverted here the task would build the REAL
        # ClaudeSDKClient (and resolve the registry) and never post
        # the ok-NOTIFICATION. Hold the with-block open over the poll loop.
        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient), \
             patch("plugin_registry.resolve_for",
                   return_value=ResolutionResult(registry_valid=True)):
            await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "x", "context": "",
                    "mode": "sync",
                }),
                _origin(),
            )

            # Poll the queue briefly for the NOTIFICATION.
            found = None
            for _ in range(50):
                if not bus.queues["assistant"].empty():
                    _pri, _seq, m = await bus.queues["assistant"].get()
                    if m.type == MessageType.NOTIFICATION:
                        found = m
                        break
                await asyncio.sleep(0.02)
            assert found is not None
            assert isinstance(found.content, DelegationComplete)
            assert found.content.status == "ok"
            assert found.content.text == "late result"


# ---------------------------------------------------------------------------
# TestAsyncMode
# ---------------------------------------------------------------------------


class TestAsyncMode:
    async def test_returns_pending_immediately(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

        _FakeSpecialistClient.reset(response="async reply", delay=0.05)
        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient), \
             patch("plugin_registry.resolve_for",
                   return_value=ResolutionResult(registry_valid=True)):
            t0 = asyncio.get_event_loop().time()
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "x", "context": "",
                    "mode": "async",
                }),
                _origin(),
            )
            t1 = asyncio.get_event_loop().time()
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "pending"
            assert payload["mode"] == "async"
            # Returned without waiting for the specialist body.
            assert (t1 - t0) < 0.04

            # Wait for background completion (keep patch active so the
            # specialist task uses the fake, not the real SDK).
            await asyncio.sleep(0.15)

            # Verify a NOTIFICATION landed.
            assert not bus.queues["assistant"].empty()


# ---------------------------------------------------------------------------
# TestCancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_caller_cancel_cancels_specialist_task(self, tmp_path):
        """If the outer turn is cancelled (voice barge-in), the in-flight
        specialist task must be cancelled too — no NOTIFICATION posts."""
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)
        cm = ChannelManager()
        init_tools(cm, bus, reg, agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})

        _FakeSpecialistClient.reset(response="slow", delay=1.0)

        async def _invoke():
            with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
                return await _with_origin(
                    delegate_to_agent.handler({
                        "agent": "finance", "task": "x", "context": "",
                        "mode": "sync",
                    }),
                    _origin(),
                )

        invocation = asyncio.create_task(_invoke())
        await asyncio.sleep(0.05)      # let it enter asyncio.wait
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation

        # No notifications posted — specialist was cancelled.
        await asyncio.sleep(0.05)
        assert bus.queues["assistant"].empty()


# ---------------------------------------------------------------------------
# TestMcpRegistryWiring — v0.6.1: specialist MCP servers resolved via registry
# ---------------------------------------------------------------------------


class TestMcpRegistryWiring:
    """`init_tools` accepts an optional `mcp_registry`; when passed,
    `_build_specialist_options` resolves `cfg.mcp_server_names` via the
    registry instead of hardcoding `mcp_servers={}`. This is the hook
    Phase 3.4 needs to make Alex's `n8n-workflows` + `casa-framework`
    tools available when he's flipped `enabled: true`."""

    async def test_mcp_registry_not_bound_degrades_to_empty(self, tmp_path):
        """Legacy 3-arg call — mcp_registry None — must not crash.
        Specialist options come back with empty mcp_servers."""
        from tools import _build_specialist_options, init_tools

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)  # no mcp_registry → None default

        cfg = _specialist_cfg(role="finance")
        cfg.mcp_server_names = ["n8n-workflows", "casa-framework"]
        options = _build_specialist_options(cfg)
        assert options.mcp_servers == {}

    async def test_mcp_registry_bound_resolves_to_registry_output(
        self, tmp_path,
    ):
        """When `mcp_registry` is passed, resolve() wins and its
        returned dict is passed straight through."""
        from mcp_registry import McpServerRegistry
        from tools import _build_specialist_options, init_tools

        mcp = McpServerRegistry()
        # Register a dummy SDK server so resolve() has something to return.
        mcp.register_sdk("casa-framework", {"type": "stdio", "command": "x"})

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, mcp)

        cfg = _specialist_cfg(role="finance")
        cfg.mcp_server_names = ["casa-framework"]
        options = _build_specialist_options(cfg)
        assert "casa-framework" in options.mcp_servers

    async def test_mcp_registry_bound_but_empty_names_yields_empty(
        self, tmp_path,
    ):
        """Specialist YAML with no `mcp_server_names` → empty mcp_servers,
        regardless of registry state. No exception."""
        from mcp_registry import McpServerRegistry
        from tools import _build_specialist_options, init_tools

        mcp = McpServerRegistry()
        mcp.register_sdk("casa-framework", {"type": "stdio", "command": "x"})

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, mcp)

        cfg = _specialist_cfg(role="finance")
        cfg.mcp_server_names = []  # specialist declares no MCP deps
        options = _build_specialist_options(cfg)
        assert options.mcp_servers == {}

    async def test_specialist_resolves_after_interactive_grants_are_added(
        self, tmp_path,
    ):
        from mcp_registry import McpServerRegistry
        from tools import _build_specialist_options, init_tools

        mcp = McpServerRegistry()
        mcp.register_sdk_factory(
            "casa-framework",
            lambda role, grants: {
                "type": "sdk",
                "instance": object(),
                "resolved_role": role,
                "resolved_grants": grants,
            },
        )
        reg = SpecialistRegistry(
            str(tmp_path / "ex"),
            tombstone_path=str(tmp_path / "del.json"),
        )
        init_tools(ChannelManager(), MessageBus(), reg, mcp)
        cfg = _specialist_cfg(role="finance")
        cfg.tools = ToolsConfig(
            allowed=["Skill", "mcp__casa-framework__recall_memory"],
            permission_mode="acceptEdits",
            skills="none",
        )
        cfg.mcp_server_names = ["casa-framework"]

        options = _build_specialist_options(
            cfg,
            resolution=ResolutionResult(registry_valid=True),
            extra_casa_tools=(
                "mcp__casa-framework__query_engager",
                "mcp__casa-framework__emit_completion",
            ),
        )

        server = options.mcp_servers["casa-framework"]
        assert server["resolved_role"] == "finance"
        assert server["resolved_grants"] == frozenset({
            "mcp__casa-framework__recall_memory",
            "mcp__casa-framework__query_engager",
            "mcp__casa-framework__emit_completion",
        })
        assert options.allowed_tools == [
            "mcp__casa-framework__recall_memory",
            "mcp__casa-framework__query_engager",
            "mcp__casa-framework__emit_completion",
        ]
        assert options.skills is None


# ---------------------------------------------------------------------------
# TestMergedRoleMap — Task 7: delegate_to_agent resolves resident configs
# ---------------------------------------------------------------------------


class TestMergedRoleMap:
    async def test_delegate_to_agent_resolves_resident(self, tmp_path, monkeypatch):
        """delegate_to_agent(agent='butler', ...) finds a resident config."""
        import tools

        # Build a butler resident cfg using the existing helper
        resident_cfg = _specialist_cfg(role="butler")
        resident_cfg.character.name = "Tina"

        reg = SpecialistRegistry(
            str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=reg,
            mcp_registry=None,
            agent_role_map={
                "butler": resident_cfg,
                "assistant": _caller_cfg(delegates=("butler",)),
            },
        )

        import agent as agent_mod
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
        })
        try:
            async def _fake_run(
                cfg, task_text, context_text, resolution=None, output_format=None,
            ):
                assert output_format is None
                return tools.DelegatedOutput(text=f"Tina says ok: {task_text}")
            monkeypatch.setattr(tools, "_run_delegated_agent", _fake_run)

            result = await tools.delegate_to_agent.handler({
                "agent": "butler",
                "task": "turn off the lights",
                "context": "",
                "mode": "sync",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "ok"
            assert payload["agent"] == "butler"
            assert "Tina says ok" in payload["text"]
        finally:
            agent_mod.origin_var.reset(token)

    async def test_delegate_to_agent_unknown_returns_unknown_agent(
        self, tmp_path, monkeypatch,
    ):
        import tools

        reg = SpecialistRegistry(
            str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=reg,
            mcp_registry=None,
            agent_role_map={"assistant": _caller_cfg(delegates=("ghost",))},
        )

        import agent as agent_mod
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
        })
        try:
            result = await tools.delegate_to_agent.handler({
                "agent": "ghost",
                "task": "anything",
                "context": "",
                "mode": "sync",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "error"
            assert payload["kind"] == "unknown_agent"
        finally:
            agent_mod.origin_var.reset(token)

    async def test_delegate_to_agent_interactive_rejected_for_resident(
        self, tmp_path, monkeypatch,
    ):
        import tools, agent as agent_mod

        resident_cfg = _specialist_cfg(role="butler")
        resident_cfg.character.name = "Tina"
        resident_cfg.channels = ["voice"]   # marker that it's a resident

        spec_reg = SpecialistRegistry(
            specialists_dir=str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=spec_reg,
            mcp_registry=None,
            agent_role_map={
                "butler": resident_cfg,
                "assistant": _caller_cfg(delegates=("butler",)),
            },
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
        })
        try:
            result = await tools.delegate_to_agent.handler({
                "agent": "butler",
                "task": "x",
                "context": "",
                "mode": "interactive",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "error"
            assert payload["kind"] == "interactive_not_supported"
        finally:
            agent_mod.origin_var.reset(token)

    async def test_delegate_to_agent_depth_cap(self, tmp_path, monkeypatch):
        """A nested delegate_to_agent (depth >= 1) returns delegation_depth_exceeded."""
        import tools, agent as agent_mod

        resident_cfg = _specialist_cfg(role="butler")
        resident_cfg.character.name = "Tina"
        resident_cfg.channels = ["voice"]
        # Declare the target so the A1 ACL passes and the depth-cap branch
        # (which runs AFTER the ACL) is the one that fires.
        resident_cfg.delegates = [DelegateEntry(agent="butler", purpose="p", when="w")]

        from specialist_registry import SpecialistRegistry
        spec_reg = SpecialistRegistry(
            specialists_dir=str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=spec_reg,
            mcp_registry=None,
            agent_role_map={"butler": resident_cfg},
        )
        # Set origin AT depth=1 (simulating that we are already inside a
        # delegated turn).
        token = agent_mod.origin_var.set({
            "role": "butler", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
            "delegation_depth": 1,
        })
        try:
            result = await tools.delegate_to_agent.handler({
                "agent": "butler",
                "task": "nested",
                "context": "",
                "mode": "sync",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "error"
            assert payload["kind"] == "delegation_depth_exceeded"
        finally:
            agent_mod.origin_var.reset(token)
