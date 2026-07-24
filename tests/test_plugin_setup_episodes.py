"""v0.112.0 — durable post-consent setup episodes (casa-plugin-elevenlabs#2).

Round-ledger model (impl review r1): membership registered at PROMPT time,
settlement = all members decided (deny/expiry settle too — never ack
counting), approvals keyed by ack generation (re-consent mints a new
episode), single-lock settlement, unambiguous server binding, terminal-state
decay/supersession.
"""

from __future__ import annotations

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
    """Configure the module against fakes + a tmp store."""
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_worker_task", None)
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)

    state = {
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
        resolve_registry_entry=lambda plugin: state["entry"],
        sleep=fake_sleep,
    )
    return state


async def _drain_pending(state):
    for ep in pse.episodes("pending"):
        await pse._run_episode(ep)


def _prompt(plugin="elevenlabs", artifact="art-1", identity="id-a"):
    pse.register_prompt(plugin=plugin, artifact_id=artifact,
                        identity=identity)


async def _decide(plugin="elevenlabs", artifact="art-1", identity="id-a",
                  approved=True, gen="g1"):
    await pse.on_consent_decision(
        plugin=plugin, artifact_id=artifact, identity=identity,
        approved=approved, approval_gen=gen if approved else "")


# ---------------------------------------------------------------------------
# Settlement (round ledger)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_prompt_approve_settles(wired):
    _prompt()
    await _decide()
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["plugin"] == "elevenlabs"
    assert eps[0]["approved_identities"] == ["id-a#g1"]
    # round consumed
    assert pse._load()["rounds"] == {}


@pytest.mark.asyncio
async def test_round_waits_for_all_members(wired):
    _prompt(identity="id-a")
    _prompt(identity="id-b")
    await _decide(identity="id-a")
    assert pse.episodes() == []           # id-b still open
    await _decide(identity="id-b", gen="g2")
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g1", "id-b#g2"]


@pytest.mark.asyncio
async def test_deny_last_settles_with_earlier_approve(wired):
    # THE impl-r1 hole: denial persists no ack — settlement must come from
    # the round ledger, and here it does: the deny DECIDES member b.
    _prompt(identity="id-a")
    _prompt(identity="id-b")
    await _decide(identity="id-a", approved=True)
    assert pse.episodes() == []
    await _decide(identity="id-b", approved=False)
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g1"]


@pytest.mark.asyncio
async def test_deny_only_round_notes_and_skips(wired):
    _prompt()
    await _decide(approved=False)
    assert pse.episodes() == []
    assert any("no approved triggers" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_reprompt_reopens_member(wired):
    # expiry decided the member; the next reconcile re-prompts → the round
    # must WAIT for the fresh decision again.
    _prompt(identity="id-a")
    _prompt(identity="id-b")
    await _decide(identity="id-b", approved=False)   # expired
    _prompt(identity="id-b")                          # re-prompted
    await _decide(identity="id-a", approved=True)
    assert pse.episodes() == []                       # id-b open again
    await _decide(identity="id-b", approved=True, gen="g9")
    assert len(pse.episodes("pending")) == 1


@pytest.mark.asyncio
async def test_reconsent_new_generation_mints_new_episode(wired):
    # impl-r1: identical tuple, NEW approval generation → new episode key.
    _prompt()
    await _decide(gen="g1")
    await _drain_pending(wired)
    assert pse.episodes()[0]["status"] == "dispatched"
    _prompt()                                         # revoke → re-prompt
    await _decide(gen="g2")                           # re-approve, new gen
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g2"]


@pytest.mark.asyncio
async def test_round_ledger_survives_restart(wired, tmp_path, monkeypatch):
    # impl-r1: durable ACROSS the consent round — approve a, "restart"
    # (fresh module state, same store file), deny b → episode still fires.
    _prompt(identity="id-a")
    _prompt(identity="id-b")
    await _decide(identity="id-a", approved=True)
    monkeypatch.setattr(pse, "_lock", None)   # simulate process restart
    monkeypatch.setattr(pse, "_kick", None)
    pse.configure(
        dispatch=lambda *a: None, notify_operator=None,
        resolve_registry_entry=lambda plugin: wired["entry"])
    await _decide(identity="id-b", approved=False)
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g1"]


@pytest.mark.asyncio
async def test_unprompted_decision_synthesizes_round(wired):
    # A decision with no registered prompt (store reset) is never dropped.
    await _decide()
    assert len(pse.episodes("pending")) == 1


@pytest.mark.asyncio
async def test_new_artifact_resets_round(wired):
    _prompt(artifact="art-OLD", identity="id-old")
    _prompt(artifact="art-1", identity="id-new")      # new generation
    await _decide(artifact="art-1", identity="id-new")
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["artifact_id"] == "art-1"
    assert eps[0]["approved_identities"] == ["id-new#g1"]


@pytest.mark.asyncio
async def test_stale_artifact_decision_ignored(wired):
    # impl r2 (both reviewers): a LATE decision from a superseded artifact
    # must never replace the current round. prompt A(art-OLD) → update →
    # prompt B(art-1) → late art-OLD decision → ignored; art-1 completes.
    _prompt(artifact="art-OLD", identity="id-old")
    _prompt(artifact="art-1", identity="id-new")      # prompt path resets
    await _decide(artifact="art-OLD", identity="id-old", approved=False)
    rounds = pse._load()["rounds"]
    assert rounds["elevenlabs"]["artifact_id"] == "art-1"   # round intact
    assert pse.episodes() == []
    await _decide(artifact="art-1", identity="id-new", approved=True)
    eps = pse.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-new#g1"]


@pytest.mark.asyncio
async def test_consumed_key_replay_never_recreates(wired):
    # impl r2 (Sol): a replayed stale generation must not recreate its
    # consumed episode and prune the current one — tombstoned keys refuse
    # the claim.
    _prompt()
    await _decide(gen="g1")
    await _drain_pending(wired)
    _prompt()
    await _decide(gen="g2")                           # supersedes g1
    eps = pse.episodes()
    assert len(eps) == 1 and eps[0]["approved_identities"] == ["id-a#g2"]
    await _decide(gen="g1")                           # stale replay
    eps = pse.episodes()
    assert len(eps) == 1
    assert eps[0]["approved_identities"] == ["id-a#g2"]   # g2 survives


@pytest.mark.asyncio
async def test_settlement_without_setup_tool_is_noop(wired):
    wired["entry"] = dict(wired["entry"], setup_tool=None)
    _prompt(plugin="gmail", artifact="art-9", identity="id-x")
    await _decide(plugin="gmail", artifact="art-9", identity="id-x")
    assert pse.episodes() == []


@pytest.mark.asyncio
async def test_new_episode_supersedes_old_ones(wired):
    _prompt()
    await _decide(gen="g1")
    await _drain_pending(wired)
    _prompt()
    await _decide(gen="g2")
    eps = pse.episodes()
    assert len(eps) == 1                              # old pruned
    assert eps[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_targets_assistant_with_exact_tool(wired):
    _prompt()
    await _decide()
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
    _prompt()
    await _decide()
    await _drain_pending(wired)
    role, text, _ = wired["dispatches"][0]
    assert role == "assistant"
    assert "'finance'" in text and "Delegate" in text
    assert "do not substitute" in text


@pytest.mark.asyncio
async def test_ambiguous_server_binding_fails_episode(wired):
    # impl-r1: zero or several server grants → FAIL with reason, never an
    # unqualified or guessed namespaced name.
    wired["entry"] = dict(wired["entry"], granted_tools=[
        "mcp__plugin_x_a", "mcp__plugin_x_b"])
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    ep = pse.episodes()[0]
    assert ep["status"] == "failed"
    assert "ambiguous" in ep["last_error"]


@pytest.mark.asyncio
async def test_stale_artifact_never_fires(wired):
    _prompt()
    await _decide()
    wired["entry"] = dict(wired["entry"], artifact_id="art-2")  # superseded
    await _drain_pending(wired)
    assert wired["dispatches"] == []
    assert pse.episodes()[0]["status"] == "stale"
    assert any("dropped" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_dispatch_failure_retries_then_notes(wired):
    wired["dispatch_ok"] = False
    _prompt()
    await _decide()
    await _drain_pending(wired)
    assert len(wired["dispatches"]) == 3              # bounded retries
    assert pse.episodes()[0]["status"] == "failed"
    assert any("manually" in n for n in wired["notes"])


@pytest.mark.asyncio
async def test_boot_redispatch_of_pending_episode(wired):
    _prompt()
    await _decide()
    raw = json.loads(pse.STORE_PATH.read_text())
    assert raw["episodes"][0]["status"] == "pending"
    await _drain_pending(wired)                       # boot-kicked drain
    assert pse.episodes()[0]["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_issues_surface_and_decay(wired, monkeypatch):
    wired["dispatch_ok"] = False
    _prompt()
    await _decide()
    await _drain_pending(wired)
    rows = pse.health_issues()
    assert rows and rows[0]["kind"] == "setup_episode_failed"
    # decay: age the failure past the window → no longer surfaced
    import time as _time
    far_future = _time.time() + pse._HEALTH_DECAY_S + 10.0
    monkeypatch.setattr(pse, "_now", lambda: far_future)
    assert pse.health_issues() == []
