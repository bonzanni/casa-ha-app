"""recall_memory tool recalls the shared bank, filtered by channel clearance."""
import json

import pytest

import agent as agent_mod
import tools

pytestmark = [pytest.mark.unit]


def _one_hit(text="a fact", provenance=None, application_tags=()):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=application_tags, provenance=provenance, backend_id="b1",
        document_id=None, chunk_id=None, source_fact_ids=None, metadata=None,
        context=None, score=None,
    )


class _RecordingSem:
    def __init__(self, hits=None):
        self.calls = []
        self._hits = (_one_hit(),) if hits is None else hits

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.calls.append({"bank": bank, "tags": sorted(tags), "budget": budget,
                           "clearance": clearance})
        return self._hits


async def test_voice_recall_uses_shared_bank_and_friends_clearance(monkeypatch):
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({"role": "butler", "channel": "voice"})
    try:
        out = await tools.recall_memory.handler({"query": "thermostat"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["bank"] == "casa"
    assert sem.calls[0]["tags"] == ["friends", "public"]
    assert sem.calls[0]["budget"] == "low"
    assert out["content"][0]["text"]


async def test_telegram_recall_sees_all_tiers(monkeypatch):
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        await tools.recall_memory.handler({"query": "salary"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["bank"] == "casa"
    assert sem.calls[0]["tags"] == ["family", "friends", "private", "public"]
    assert sem.calls[0]["budget"] == "mid"


async def test_webhook_trigger_recall_is_public_only(monkeypatch):
    """Release A Layer 2: a webhook_trigger turn recalls at PUBLIC clearance
    only, never the webhook channel's historical private tier."""
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "webhook",
        "_origin_route": "webhook_trigger", "_origin_clearance": "public",
    })
    try:
        await tools.recall_memory.handler({"query": "salary"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["tags"] == ["public"]


async def test_webhook_trigger_declared_family_clearance(monkeypatch):
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "webhook",
        "_origin_route": "webhook_trigger", "_origin_clearance": "family",
    })
    try:
        await tools.recall_memory.handler({"query": "x"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["tags"] == ["family", "friends", "public"]


async def test_invoke_recall_sees_all_tiers(monkeypatch):
    """Operator-signed /invoke keeps private clearance."""
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "webhook", "_origin_route": "invoke",
    })
    try:
        await tools.recall_memory.handler({"query": "x"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["tags"] == ["family", "friends", "private", "public"]


async def test_backend_unavailable_returns_unavailable_status(monkeypatch):
    """Three-outcome contract: a failed recall must NOT report status=ok —
    the agent would then falsely tell the user Casa has no such memory."""
    from semantic_memory import RecallUnavailable

    class _Down:
        async def recall_items(self, *a, **k): raise RecallUnavailable("http_504")

    monkeypatch.setattr(agent_mod, "active_semantic_memory", _Down(), raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        out = await tools.recall_memory.handler({"query": "thermostat"})
    finally:
        agent_mod.origin_var.reset(token)
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "unavailable"
    assert "memory" not in payload   # never a fake empty digest


async def test_unexpected_backend_error_is_unavailable_not_ok(monkeypatch):
    class _Boom:
        async def recall_items(self, *a, **k): raise RuntimeError("x")

    monkeypatch.setattr(agent_mod, "active_semantic_memory", _Boom(), raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        out = await tools.recall_memory.handler({"query": "q"})
    finally:
        agent_mod.origin_var.reset(token)
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "unavailable"


async def test_sem_none_returns_unavailable_not_ok(monkeypatch):
    """No backend wired = memory cannot be checked — never a fake zero-hit."""
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        out = await tools.recall_memory.handler({"query": "q"})
    finally:
        agent_mod.origin_var.reset(token)
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "unavailable"


async def test_zero_hit_recall_stays_ok_with_empty_memory(monkeypatch):
    """A genuine zero-hit search keeps the {status: ok, memory: ""} shape."""
    class _Empty:
        async def recall_items(self, *a, **k): return ()

    monkeypatch.setattr(agent_mod, "active_semantic_memory", _Empty(), raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        out = await tools.recall_memory.handler({"query": "q"})
    finally:
        agent_mod.origin_var.reset(token)
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "ok"
    assert payload["memory"] == ""


async def test_webhook_missing_route_recall_fail_closed_public(monkeypatch):
    """Fail-closed: a webhook turn with no origin route recalls public only."""
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "webhook"})
    try:
        await tools.recall_memory.handler({"query": "x"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["tags"] == ["public"]


async def test_direct_tool_attributes_and_never_leaks_raw_tags(monkeypatch):
    """Step 8: a private-clearance telegram recall renders attribution but the
    reserved provenance tag + bare tier tokens never escape into the result."""
    from personality_types import SpeakerProvenance

    prov = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:butler",
        persona_id="casa.personas/tina", persona_version="1.0.0",
        display_name="Tina", binding_digest="sha256:" + "5" * 64,
    )
    hit = _one_hit(text="the thermostat is 20C", provenance=prov,
                   application_tags=("house",))
    sem = _RecordingSem(hits=(hit,))
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        out = await tools.recall_memory.handler({"query": "temp?"})
    finally:
        agent_mod.origin_var.reset(token)
    text = out["content"][0]["text"]
    payload = json.loads(text)
    assert payload["status"] == "ok"
    assert "Tina previously said" in payload["memory"]
    assert "[source: resident:butler" in payload["memory"]
    assert "casa-source-" not in json.dumps(payload)
    assert "house" not in payload["memory"]  # application_tags never rendered


async def test_restricted_webhook_recall_strips_identity(monkeypatch):
    """Step 8: a webhook_trigger turn (restricted_webhook surface, public
    clearance) never names a person and never leaks raw tags."""
    from personality_types import SpeakerProvenance

    prov = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:butler",
        persona_id="casa.personas/tina", persona_version="1.0.0",
        display_name="Tina", binding_digest="sha256:" + "5" * 64,
    )
    hit = _one_hit(text="a public fact", provenance=prov)
    sem = _RecordingSem(hits=(hit,))
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "webhook",
        "_origin_route": "webhook_trigger", "_origin_clearance": "public",
    })
    try:
        out = await tools.recall_memory.handler({"query": "x"})
    finally:
        agent_mod.origin_var.reset(token)
    payload = json.loads(out["content"][0]["text"])
    assert "a public fact" in payload["memory"]
    assert "Tina" not in payload["memory"]
    assert "resident:butler" not in payload["memory"]
    assert "casa-source-" not in json.dumps(payload)
