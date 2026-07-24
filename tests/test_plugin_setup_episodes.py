"""v0.112.0 — durable post-consent setup episodes (casa-plugin-elevenlabs#2).

Covers: manifest contract, settlement over all terminal decisions (incl. the
deny-last suppression hole), atomic episode claim, worker dispatch with
exact-artifact TOCTOU guard, deterministic target selection, retry/failure
paths, boot re-dispatch, and health-issue surfacing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import plugin_setup_episodes as pse
from plugin_store import StoreError, manifest_setup_tool


# ---------------------------------------------------------------------------
# Manifest contract
# ---------------------------------------------------------------------------

def test_manifest_setup_tool_absent_is_none():
    assert manifest_setup_tool({}) is None
    assert manifest_setup_tool({"casa": {}}) is None
    assert manifest_setup_tool({"casa": "nope"}) is None


def test_manifest_setup_tool_valid():
    m = {"casa": {"setupTool": "setup_elevenlabs_voicemail"}}
    assert manifest_setup_tool(m) == "setup_elevenlabs_voicemail"


@pytest.mark.parametrize("bad", [
    "", "voicemail_setup", "setup_", "setup_Voicemail", "setup_a b",
    "setup_ünïcode", "setup_" + "x" * 65, 7, None, ["setup_x"],
])
def test_manifest_setup_tool_malformed_refuses(bad):
    with pytest.raises(StoreError) as exc:
        manifest_setup_tool({"casa": {"setupTool": bad}})
    assert exc.value.reason_code == "setup_tool_invalid"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Configure the module against fakes + a tmp store; returns the mutable
    seam state."""
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_open_decisions", {})
    monkeypatch.setattr(pse, "_worker_task", None)

    state = {
        "pending": 0,
        "entry": {
            "artifact_id": "art-1",
            "targets": ["resident:assistant"],
            "granted_tools": ["mcp__plugin_elevenlabs_elevenlabs"],
            "setup_tool": "setup_elevenlabs_voicemail",
        },
        "dispatches": [],
        "dispatch_ok": True,
        "notes": [],
        "sleeps": [],
    }

    async def dispatch(role, text, context):
        state["dispatches"].append((role, text, context))
        return state["dispatch_ok"]

    async def notify(text):
        state["notes"].append(text)

    async def fake_sleep(s):
        state["sleeps"].append(s)

    pse.configure(
        dispatch=dispatch, notify_operator=notify,
        pending_consents_for=lambda plugin: state["pending"],
        resolve_registry_entry=lambda plugin: state["entry"],
        sleep=fake_sleep,
    )
    return state


async def _drain_pending(state):
    for ep in pse.episodes("pending"):
        await pse._run_episode(ep)


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_approve_settles_and_creates_episode(wired):
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["plugin"] == "elevenlabs"
    assert eps[0]["artifact_id"] == "art-1"
    assert eps[0]["setup_tool"] == "setup_elevenlabs_voicemail"
    assert eps[0]["approved_identities"] == ["id-a"]


@pytest.mark.asyncio
async def test_no_episode_while_other_consents_pending(wired):
    wired["pending"] = 1
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    assert pse.episodes() == []
    # second trigger approved → episode covers BOTH identities
    wired["pending"] = 0
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-b",
        approved=True)
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a", "id-b"]


@pytest.mark.asyncio
async def test_deny_last_still_fires_for_earlier_approve(wired):
    # The design-round hole: Approve A (B pending) then Deny B — settlement
    # is evaluated on the DENY and the approved subset dispatches.
    wired["pending"] = 1
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    assert pse.episodes() == []
    wired["pending"] = 0
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-b",
        approved=False)
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a"]


@pytest.mark.asyncio
async def test_deny_only_settlement_notes_and_skips(wired):
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=False)
    assert pse.episodes() == []
    assert any("no approved triggers" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_settlement_without_setup_tool_is_noop(wired):
    wired["entry"] = dict(wired["entry"], setup_tool=None)
    await pse.on_consent_decision(
        plugin="gmail", artifact_id="art-9", identity="id-x", approved=True)
    assert pse.episodes() == []


@pytest.mark.asyncio
async def test_duplicate_settlement_claims_once(wired):
    for _ in range(2):
        await pse.on_consent_decision(
            plugin="elevenlabs", artifact_id="art-1", identity="id-a",
            approved=True)
    assert len(pse.episodes()) == 1


@pytest.mark.asyncio
async def test_new_artifact_resets_open_accumulator(wired):
    wired["pending"] = 1
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-OLD", identity="id-old",
        approved=True)
    wired["pending"] = 0
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-new",
        approved=True)
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-new"]   # old gen dropped
    assert eps[0]["artifact_id"] == "art-1"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_targets_assistant_with_exact_tool(wired):
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    await _drain_pending(wired)
    assert len(wired["dispatches"]) == 1
    role, text, ctx = wired["dispatches"][0]
    assert role == "assistant"
    assert "mcp__plugin_elevenlabs_elevenlabs__setup_elevenlabs_voicemail" in text
    assert ctx["synthetic"] == "plugin_setup"
    ep = pse.episodes()[0]
    assert ep["status"] == "dispatched"
    assert ctx["setup_episode"] == ep["id"]


@pytest.mark.asyncio
async def test_specialist_only_target_delegates_via_assistant(wired):
    wired["entry"] = dict(wired["entry"], targets=["specialist:finance"])
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    await _drain_pending(wired)
    role, text, _ = wired["dispatches"][0]
    assert role == "assistant"
    assert "'finance'" in text and "Delegate" in text
    assert "do not substitute" in text


@pytest.mark.asyncio
async def test_stale_artifact_never_fires(wired):
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    wired["entry"] = dict(wired["entry"], artifact_id="art-2")  # superseded
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    assert pse.episodes()[0]["status"] == "stale"
    assert any("dropped" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_dispatch_failure_retries_then_notes(wired):
    wired["dispatch_ok"] = False
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    await _drain_pending(wired)
    assert len(wired["dispatches"]) == 3          # bounded retries
    ep = pse.episodes()[0]
    assert ep["status"] == "failed"
    assert any("manually" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_boot_redispatch_of_pending_episode(wired, tmp_path):
    # Simulate a crash: episode persisted pending, process restarts —
    # a fresh drain (what the boot-kicked worker does) dispatches it.
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    raw = json.loads(pse.STORE_PATH.read_text())
    assert raw["episodes"][0]["status"] == "pending"
    await _drain_pending(wired)
    assert pse.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_health_issues_surface_non_success(wired):
    wired["dispatch_ok"] = False
    await pse.on_consent_decision(
        plugin="elevenlabs", artifact_id="art-1", identity="id-a",
        approved=True)
    await _drain_pending(wired)
    rows = pse.health_issues()
    assert len(rows) == 1
    assert rows[0]["kind"] == "setup_episode_failed"
    assert rows[0]["plugin"] == "elevenlabs"
