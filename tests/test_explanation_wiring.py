"""GH #199: the per-turn explanation writer is wired into Agent._process.

Task 14 shipped ExplanationStore + the /admin/explain route + `casactl explain`,
but nothing called ``record()`` — every correlation id 404'd. These tests drive
the REAL production turn path (Agent._process via the SDK-client pool, with the
CLI subprocess stubbed the way sibling agent-turn tests do) and prove:

* a record lands under the turn's ACTUAL correlation id (the one an operator
  reads from logs / cid_var), so ``casactl explain <cid>`` round-trips to a 200;
* the record honestly captures the resolved model, the selected prompt
  projection, the prompt/binding digests, and the auto-recall's tiers +
  casa-source-free attribution labels;
* an explanation-store failure never fails or delays the user's reply.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import agent as agent_mod
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from explanation_store import ExplanationStore
from log_cid import cid_var
from mcp_registry import McpServerRegistry
from personality_binding import BindingRecord
from personality_types import RecallHit, SpeakerProvenance
from prompt_compiler import CompiledProjection, CompiledPromptBundle
from session_registry import SessionRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


class _FakeClient:
    """Minimal stand-in for the CLI subprocess client (mirrors the sibling
    agent-turn tests): opens, accepts a query, yields no messages, returns a
    fixed session id."""

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, text):
        return None

    async def receive_response(self):
        return
        yield  # pragma: no cover

    @property
    def session_id(self):
        return "sid-explain"


def _binding() -> BindingRecord:
    return BindingRecord(
        stable_agent_id="resident:assistant", role_checksum="rc-assistant",
        mode="component-default", persona_id="ellen", persona_version="3",
        persona_checksum="pc-ellen", compiler_schema_version="v1",
        dependency_digests=("dep-a", "dep-b"), effective_config_digest="ecd-ellen",
        binding_digest="bd-ellen",
    )


def _bundle() -> CompiledPromptBundle:
    text = CompiledProjection(
        system_prompt="TEXT PROJECTION — Ellen persona prose",
        digest="digest-text", estimated_tokens=321,
    )
    voice = CompiledProjection(
        system_prompt="VOICE PROJECTION", digest="digest-voice", estimated_tokens=111,
    )
    restricted = CompiledProjection(
        system_prompt="RESTRICTED PROJECTION", digest="digest-restricted",
        estimated_tokens=42,
    )
    return CompiledPromptBundle(
        role_id="resident:assistant", resolved_model="claude-sonnet-4-6",
        text=text, voice=voice, restricted_webhook=restricted,
        binding_digest="bd-ellen",
    )


def _resident_config(semantic_hint="You are Ellen (legacy).") -> AgentConfig:
    binding = _binding()
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT,
        role="assistant",
        model="claude-sonnet-4-6",
        system_prompt=semantic_hint,
        character=CharacterConfig(name="Ellen"),
        tools=ToolsConfig(allowed=[]),
        memory=MemoryConfig(token_budget=1000),
        role_id="resident:assistant",
        kind="resident",
        role_checksum="rc-assistant",
        binding=binding,
        compiled_prompt_bundle=_bundle(),
        binding_digest="bd-ellen",
    )


def _make_agent(tmp_path, *, semantic_memory) -> agent_mod.Agent:
    return agent_mod.Agent(
        config=_resident_config(),
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=semantic_memory,
    )


def _admin_app(store: ExplanationStore) -> web.Application:
    from personality_admin_handlers import register_personality_admin_routes

    runtime = SimpleNamespace(
        explanation_store=store, persona_packs={}, compiled_prompt_bundles={},
        role_slots={}, bindings={},
    )
    app = web.Application()
    register_personality_admin_routes(app, runtime=runtime)
    return app


def _drive_turn(agent, monkeypatch, store, *, cid, semantic_memory):
    """Run one telegram turn through the production _process path with the CLI
    stubbed and the module-global runtime pointed at ``store``."""
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(explanation_store=store), raising=False,
    )
    token = cid_var.set(cid)
    try:
        async def run():
            with patch("sdk_client_pool._default_make_client", _FakeClient):
                msg = BusMessage(
                    type=MessageType.REQUEST, source="telegram",
                    target="assistant", content="what did we decide?",
                    channel="telegram", context={"chat_id": "42"},
                )
                await agent._process(msg, on_token=None)

        asyncio.run(run())
    finally:
        cid_var.reset(token)


def test_turn_records_explanation_roundtrips_via_admin_route(tmp_path, monkeypatch):
    """The record lands under the turn's real cid → GET /admin/explain returns
    200 with the honest projection/model/binding, sensitive fields stripped."""
    store = ExplanationStore(tmp_path / "explanations")
    sm = AsyncMock()
    sm.profile.return_value = ""
    # No memory backend hits: a fresh text turn's auto-recall runs but finds an
    # unavailable backend → the record's memory fields stay empty (honest).
    from semantic_memory import RecallUnavailable
    sm.recall_items.side_effect = RecallUnavailable("not_configured")
    agent = _make_agent(tmp_path, semantic_memory=sm)

    cid = "c-explain-roundtrip-1"
    _drive_turn(agent, monkeypatch, store, cid=cid, semantic_memory=sm)

    # The store now holds the record (proves record() was called on the turn).
    raw = store.get(cid, show_sensitive=False)
    assert raw["role_id"] == "resident:assistant"
    assert raw["resolved_model"] == "claude-sonnet-4-6"
    assert raw["projection"] == "text"
    assert raw["static_prompt_digest"] == "digest-text"
    assert raw["static_prompt_estimated_tokens"] == 321
    assert raw["binding_digest"] == "bd-ellen"
    assert raw["persona_ref"] == "ellen@3"
    assert raw["dependency_digests"] == ["dep-a", "dep-b"]
    # Sensitive prose fields stripped by default (tier-token gating is #202).
    assert "system_prompt" not in raw
    assert "memory_text" not in raw

    async def check_route():
        app = _admin_app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/admin/explain", json={"correlation_id": cid},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["projection"] == "text"
            assert body["role_id"] == "resident:assistant"

    asyncio.run(check_route())


def test_turn_records_memory_attributions_without_reserved_tags(tmp_path, monkeypatch):
    """A successful auto-recall records tiers + honest attribution labels; the
    hit's reserved casa-source- application tag never enters the record (the
    store would reject it), and the tiers are gated behind show_sensitive."""
    store = ExplanationStore(tmp_path / "explanations")
    hit = RecallHit(
        text="we decided to ship Friday",
        memory_type="experience",
        sensitivity="family",
        application_tags=("casa-source-v1.deadbeef",),  # reserved — must not leak
        provenance=SpeakerProvenance(
            speaker_kind="resident", role_id="assistant",
            persona_id="ellen", persona_version="3", display_name="Ellen",
        ),
        backend_id="b1", document_id="d1", chunk_id="c1",
        source_fact_ids=("f1",), metadata=None, context=None, score=0.9,
    )
    sm = AsyncMock()
    sm.profile.return_value = ""
    sm.recall_items.return_value = (hit,)
    agent = _make_agent(tmp_path, semantic_memory=sm)

    cid = "c-explain-memory-1"
    _drive_turn(agent, monkeypatch, store, cid=cid, semantic_memory=sm)

    # Sensitive view: tiers present, no reserved tag anywhere.
    full = store.get(cid, show_sensitive=True)
    assert full["memory_tiers"] == ["family"]
    assert full["memory_attributions"]  # non-empty
    import json

    encoded = json.dumps(full)
    assert "casa-source-" not in encoded
    # The recorded memory_text (rendered recall) is the sensitive prose.
    assert full["memory_text"] and "decided to ship Friday" in full["memory_text"]

    # Default (non-sensitive) view still carries the honest attribution labels
    # (tier-token gating is exercised by #202's dedicated tests).
    default = store.get(cid, show_sensitive=False)
    assert default["memory_attributions"]


def test_projection_name_matches_build_options_selection():
    """The recorded projection name must track _build_options' selection: an
    untrusted webhook origin → restricted_webhook, voice → voice, else text."""
    from agent import _projection_name_for_turn

    assert _projection_name_for_turn("webhook", "webhook_trigger") == "restricted_webhook"
    assert _projection_name_for_turn("webhook", None) == "restricted_webhook"
    # An explicit server-stamped invoke webhook is a trusted origin → text.
    assert _projection_name_for_turn("webhook", "invoke") == "text"
    assert _projection_name_for_turn("voice", None) == "voice"
    assert _projection_name_for_turn("telegram", None) == "text"


def test_attribution_label_is_id_based_never_casa_source():
    """Attribution labels come from the clearance-gated provenance view — never a
    display name (PII) and never a raw casa-source- application tag."""
    from agent import _attribution_label

    def _hit(**prov):
        return RecallHit(
            text="t", memory_type="experience", sensitivity="private",
            application_tags=("casa-source-v1.tag",),
            provenance=(SpeakerProvenance(**prov) if prov else None),
            backend_id=None, document_id=None, chunk_id=None,
            source_fact_ids=None, metadata=None, context=None, score=None,
        )

    assert _attribution_label(_hit(), clearance="private") == "unattributed"
    assert _attribution_label(
        _hit(speaker_kind="user", user_id="u1", display_name="Nicola"),
        clearance="private",
    ) == "user"
    label = _attribution_label(
        _hit(speaker_kind="resident", role_id="assistant",
             persona_id="ellen", persona_version="3", display_name="Ellen"),
        clearance="private",
    )
    assert label == "resident:assistant/ellen@3"
    assert "casa-source-" not in label
    assert "Ellen" not in label


def test_explanation_store_failure_never_fails_the_turn(tmp_path, monkeypatch):
    """A store that raises on record() must not propagate into the user turn."""

    class _ExplodingStore:
        def record(self, record):
            raise RuntimeError("disk full")

    sm = AsyncMock()
    sm.profile.return_value = ""
    from semantic_memory import RecallUnavailable
    sm.recall_items.side_effect = RecallUnavailable("not_configured")
    agent = _make_agent(tmp_path, semantic_memory=sm)

    monkeypatch.setattr(
        agent_mod, "active_runtime",
        SimpleNamespace(explanation_store=_ExplodingStore()), raising=False,
    )
    token = cid_var.set("c-explain-explode-1")
    try:
        async def run():
            with patch("sdk_client_pool._default_make_client", _FakeClient):
                msg = BusMessage(
                    type=MessageType.REQUEST, source="telegram",
                    target="assistant", content="hi",
                    channel="telegram", context={"chat_id": "7"},
                )
                # Must complete without raising despite the store fault.
                return await agent._process(msg, on_token=None)

        # No exception escapes.
        asyncio.run(run())
    finally:
        cid_var.reset(token)
