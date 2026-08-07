"""#451 — ONE runner owns plugin setup, released by a positive consent verdict.

Two attempts to classify *which* of two runners executes a plugin's setup tool
failed adversarial review, because mutation time has no third answer: a runner
must be named then and there. v0.161.0 deletes the second runner and makes the
episode facility's "hold, stay visible, re-check" the answer to every unknown.

This file is the acceptance matrix from the issue — one case per row — and it
drives the REAL reconcilers into the REAL episode worker. Only the outermost
seams are doubled (agent dispatch, the operator note, the registry entry the
worker resolves at dispatch time). The standing failure mode in this area is a
fake that hides a self-defeating bug: a doubled verdict would make every case
below pass by construction, since the verdict IS the thing under test.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import callback_reconcile as cr
import plugin_setup_episodes as pse
import trigger_reconcile as tr
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity as cb_ack_identity
from plugin_callbacks import declaration_digest
from trigger_acks import TriggerAckStore
from trigger_registry import TriggerRegistry

DECLARED = "authorize"
EFFECTIVE = "plg-gmail--authorize"
TRIGGER_AUTH = {"mode": "static_header", "header": "X-API-Key"}


# ---------------------------------------------------------------------------
# Harness — real reconcilers, real worker, doubled edges only
# ---------------------------------------------------------------------------

class _NoChannel:
    """No Telegram DM reachable. Used by the unreachable-operator row; every
    other row simply never needs a keyboard, because these tests drive consent
    through the ack stores rather than through taps (the tap path has its own
    coverage in tests/test_callback_consent.py)."""

    def get(self, name):
        return None


def _plugin(*, triggers=False, callbacks=False, setup="setup_gmail",
            artifact="art-1"):
    casa: dict = {}
    if triggers:
        casa["triggers"] = [{"name": "push", "type": "webhook",
                             "target": "resident:assistant",
                             "auth": dict(TRIGGER_AUTH)}]
    if callbacks:
        casa["callbacks"] = [{"name": DECLARED}]
    if setup:
        casa["setupTool"] = setup
    return SimpleNamespace(
        name="gmail", artifact_id=artifact, path=f"/store/gmail/{artifact}",
        version="1.0.0", manifest_name="gmail",
        manifest={"name": "gmail", "casa": casa})


@pytest.fixture
def env(monkeypatch, tmp_path):
    """One live wiring: the episode store on tmp, the worker's seams doubled."""
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)

    state = SimpleNamespace(
        dispatched=[], notes=[], plugin=_plugin(setup="setup_gmail"),
        entry={"artifact_id": "art-1", "setup_tool": "setup_gmail",
               "granted_tools": ["gmailsrv"],
               "targets": ["resident:assistant"]},
        trig_acks=TriggerAckStore(path=tmp_path / "trigger_acks.json"),
        cb_acks=CallbackAckStore(path=tmp_path / "callback_acks.json"),
        registry=TriggerRegistry(scheduler=None, app=None, bus=None),
        secrets_dir=tmp_path / "webhook_secrets",
        channel_manager=_NoChannel(),
    )

    async def _dispatch(role, instruction, ctx):
        state.dispatched.append((role, instruction, ctx))
        return True

    async def _notify(text):
        state.notes.append(text)

    pse.configure(
        dispatch=_dispatch, notify_operator=_notify,
        resolve_registry_entry=lambda p: state.entry,
        ack_lookup=lambda ident: None, routes_live=lambda p: True)
    # The health regen writes to /data — not this test's subject.
    monkeypatch.setattr(tr, "_regen_health_safe", _noop)
    monkeypatch.setattr(cr, "_regen_health_safe", _noop)
    # The union half of the sealing reads its own module's DEFAULT ack store
    # (the peer reconciler does not hold the other kind's store), so injecting
    # one kind without the other makes an already-acked consent look pending
    # forever. Point both defaults at these tmp stores.
    monkeypatch.setattr(tr, "_default_acks", lambda: state.trig_acks)
    monkeypatch.setattr(cr, "_default_acks", lambda: state.cb_acks)
    return state


async def _noop():
    return None


class _Spool:
    def ensure_plugin_dirs(self, plugin): pass
    def write_ready(self, plugin, payload): pass
    def delete_ready(self, plugin): pass
    def write_index_entry(self, path, payload): pass
    def delete_index_entry(self, path): pass
    def read_marker(self, plugin): return None
    def read_index_marker(self, path): return None



def _recording(state):
    async def _dispatch(role, instruction, ctx):
        state.dispatched.append((role, instruction, ctx))
        return True
    return _dispatch


def _swallow(state):
    async def _notify(text):
        state.notes.append(text)
    return _notify


async def _unreachable(*a, **kw):
    raise AssertionError("dispatch must not happen while setup is held")


async def _reconcile(state):
    """What every lifecycle site does: run BOTH reconcilers as a pair."""
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[state.plugin],
                               issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": state.plugin.artifact_id,
                 "targets": ["resident:assistant"]}]

    monkey_base = getattr(state, "base_url", "https://casa.example.org")
    cr._base_url_override = monkey_base
    await tr.reconcile_plugin_triggers(
        trigger_registry=state.registry, role_configs=role_configs,
        channel_manager=state.channel_manager, acks=state.trig_acks,
        secrets_dir=state.secrets_dir, prompt=True,
        resolver=_resolver, global_secret_ok=lambda: True)
    await cr.reconcile_plugin_callbacks(
        trigger_registry=state.registry, role_configs=role_configs,
        channel_manager=state.channel_manager, acks=state.cb_acks,
        spool=_Spool(), resolver=_resolver, entries=_entries, prompt=True)
    await pse._worker_pass()


def _obligation():
    rows = pse.episodes()
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def _pending_triggers(state):
    """The reconciler's OWN pending rows. Deriving the consent identity by hand
    is a trap: the row's `auth` is the NORMALIZED map (the compute fills in
    tolerance_secs and secret_owner), so a hand-built identity hashes
    differently and the ack silently never matches."""
    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[state.plugin],
                               issues=[])
    return tr.compute_desired(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=state.trig_acks, resolver=_resolver,
        global_secret_ok=lambda: True).pending


def _trigger_identity(state):
    row = _pending_triggers(state)[0]
    return tr.ack_identity(
        plugin=row["plugin"], artifact_id=row["artifact_id"],
        effective=row["effective"], target=row["target"], auth=row["auth"])


def _approve_trigger(state):
    """Persist the trigger consent ack the reconciler looks for, and record the
    approval into the round the way the consent commit step does."""
    ident = None
    for row in _pending_triggers(state):
        ident = tr.ack_identity(
            plugin=row["plugin"], artifact_id=row["artifact_id"],
            effective=row["effective"], target=row["target"],
            auth=row["auth"])
        state.trig_acks.record(
            identity=ident, plugin=row["plugin"],
            artifact_id=row["artifact_id"], effective=row["effective"],
            target=row["target"], auth=row["auth"])
        gen = str((state.trig_acks.get(ident) or {}).get("gen", ""))
        pse.record_approval_sync(plugin=row["plugin"],
                                 artifact_id=row["artifact_id"],
                                 identity=ident, gen=gen)
    assert ident is not None, "nothing was pending to approve"
    return ident


def _approve_callback(state):
    art = state.plugin.artifact_id
    digest = declaration_digest({"declared": DECLARED, "effective": EFFECTIVE})
    ident = cb_ack_identity("gmail", EFFECTIVE, digest)
    rec = state.cb_acks.record("gmail", EFFECTIVE, digest)
    pse.record_approval_sync(plugin="gmail", artifact_id=art, identity=ident,
                             gen=str(rec.get("gen", "")))
    return ident


# ---------------------------------------------------------------------------
# The eleven acceptance rows. Each asserts WHICH runner acted — and since the
# hand-back is gone, "an agent ran it" is unrepresentable: the only possible
# runners are the episode worker and nobody.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_install_with_triggers_waits_for_approval(env):
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    assert env.dispatched == []                        # never before approval
    assert _obligation()["gate"] == "awaiting_verdict"
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_fresh_install_with_callbacks_only_waits_for_approval(env):
    env.plugin = _plugin(callbacks=True)
    await _reconcile(env)
    assert env.dispatched == []
    assert _obligation()["gate"] == "awaiting_verdict"
    _approve_callback(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_no_consent_gate_dispatches_immediately(env):
    """Nothing to wait for — but it is Casa that says so, POSITIVELY, via a
    zero-member verdict. The obligation would hold if nobody had said it."""
    env.plugin = _plugin()                             # setupTool only
    await _reconcile(env)
    assert len(env.dispatched) == 1
    assert _obligation()["status"] == "dispatched"


@pytest.mark.asyncio
async def test_update_with_triggers_never_runs_before_the_new_secret(env):
    env.plugin = _plugin(triggers=True)
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1                    # installed + approved
    # An update: NEW artifact. A trigger ack is artifact-bound, so the consent
    # re-prompts and the re-minted secret does not exist yet.
    env.plugin = _plugin(triggers=True, artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    assert len(env.dispatched) == 1                    # NOT re-run yet
    assert _obligation()["artifact_id"] == "art-2"
    assert _obligation()["gate"] == "awaiting_verdict"
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 2


@pytest.mark.asyncio
async def test_update_with_callbacks_unchanged_dispatches(env):
    """#443's case. The declaration-bound ack survives, so no round opens —
    and the OLD code read that absence as "consent will run it" or as "the
    integration is dead", depending on which branch it took."""
    env.plugin = _plugin(callbacks=True)
    _approve_callback(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1
    env.plugin = _plugin(callbacks=True, artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    # The ack binds the declaration, which is byte-identical → still acked →
    # zero-member verdict → the new artifact's obligation releases at once.
    assert len(env.dispatched) == 2
    assert _obligation()["artifact_id"] == "art-2"


@pytest.mark.asyncio
async def test_update_changing_the_setup_tool_runs_the_new_one(env):
    """Attempt 2's MISSED RUN: nothing binds a setup tool's identity to a
    callback ack's identity, so a changed setupTool with an unchanged
    declaration used to keep its ack, open no round, and never run at all."""
    env.plugin = _plugin(callbacks=True)
    _approve_callback(env)
    await _reconcile(env)
    assert "setup_gmail" in env.dispatched[0][1]
    env.plugin = _plugin(callbacks=True, setup="setup_gmail_v2",
                         artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2",
                     setup_tool="setup_gmail_v2")
    await _reconcile(env)
    assert len(env.dispatched) == 2
    assert "setup_gmail_v2" in env.dispatched[1][1]


@pytest.mark.asyncio
async def test_update_changing_callbacks_never_runs_before_approval(env):
    """Attempt 2's PREMATURE RUN: a changed declaration opens a round, but the
    declaration-derived answer handed back anyway and the engager ran setup
    before the operator approved the new endpoint."""
    env.plugin = _plugin(callbacks=True)
    _approve_callback(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1
    # A DIFFERENT declared callback ⇒ different digest ⇒ the ack no longer
    # covers it ⇒ pending again.
    env.plugin = _plugin(callbacks=True, artifact="art-2")
    env.plugin.manifest["casa"]["callbacks"] = [{"name": "authorize_v2"}]
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    assert len(env.dispatched) == 1                    # held, not run
    assert _obligation()["gate"] == "awaiting_verdict"


@pytest.mark.asyncio
async def test_a_plugin_with_no_declared_setup_tool_has_no_runner(env):
    """Legacy handoff-only tools are unsupported pre-1.0: nobody runs it, and
    nothing pretends otherwise. Previously such a plugin got a MANDATORY
    hand-back and the engager ran it before approval minted the secret."""
    env.plugin = _plugin(triggers=True, setup=None)
    env.entry = dict(env.entry, setup_tool=None)
    _approve_trigger(env)
    await _reconcile(env)
    assert env.dispatched == []
    assert pse.episodes() == []                        # no obligation at all
    assert env.notes == []                             # and no spurious note


@pytest.mark.asyncio
async def test_denied_consent_refuses_and_a_reprompt_rearms(env):
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    ident = _trigger_identity(env)
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity=ident, approved=False, nonce="")
    assert env.dispatched == []
    assert _obligation()["status"] == "refused"
    note = " ".join(env.notes)
    assert "Run it manually" not in note        # the operator has no such tool
    assert "trigger(s)" not in note             # it may have been a callback
    # The way back: the next reconcile re-prompts (no ack), which re-arms.
    await _reconcile(env)
    assert _obligation()["status"] == "pending"
    assert _obligation()["gen"] == 1
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_unreachable_operator_holds_rather_than_choosing(env):
    """The defect #451 names directly: sealing used to live AFTER the
    reachability gate, so with no DM nothing was sealed — and a round could
    first seal on a later ordinary reload, long after a mutation had reported
    which runner owned setup. Now the verdict exists and carries members."""
    env.plugin = _plugin(triggers=True)
    env.channel_manager = _NoChannel()                 # no DM the whole time
    await _reconcile(env)
    assert env.dispatched == []
    assert _obligation()["gate"] == "awaiting_verdict"
    rnd = pse._load()["rounds"]["gmail"]
    assert rnd["artifact_id"] == "art-1"
    assert len(rnd["members"]) == 1                    # sealed, and NOT empty
    assert all(m["state"] == "open" for m in rnd["members"].values())
    # `pending` never decays out of health, so this stays actionable.
    assert [i["kind"] for i in pse.health_issues()] == ["setup_episode_pending"]


@pytest.mark.asyncio
async def test_a_failed_pending_compute_holds_rather_than_choosing(env):
    """The accessor-raises row. Neither direction is safe to guess, so the
    verdict is simply not sealed — and an unsealed verdict holds."""
    env.plugin = _plugin()                             # would release at once
    real_cb = cr.callback_pending_for_union
    real_tr = tr.trigger_pending_for_union

    def _boom(**kw):
        raise RuntimeError("boom")

    cr.callback_pending_for_union = _boom
    tr.trigger_pending_for_union = _boom
    try:
        await _reconcile(env)
        assert env.dispatched == []
        assert _obligation()["gate"] == "awaiting_verdict"
        assert "gmail" not in pse._load()["rounds"]    # nothing sealed
    finally:
        # Restore by hand, NOT monkeypatch.undo() — that would also revert the
        # fixture's STORE_PATH patch and send the next write at real /data.
        cr.callback_pending_for_union = real_cb
        tr.trigger_pending_for_union = real_tr
    # Recovery is automatic once the compute works again.
    await _reconcile(env)
    assert len(env.dispatched) == 1


# ---------------------------------------------------------------------------
# The invariant the eleven rows rest on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_absence_of_a_round_is_never_a_permission(env):
    """INV-PLUG-010, stated as its own red case: an obligation with NO sealed
    verdict must not dispatch, however long it waits. This is attempt 1's
    mechanism — it read "no round queued right now" as "nothing to wait for"
    and dispatched before the reconcile had opened the round."""
    assert pse.ensure_obligation(plugin="gmail", artifact_id="art-1") is True
    for _ in range(5):
        await pse._worker_pass()
    assert env.dispatched == []
    assert _obligation()["status"] == "pending"


@pytest.mark.asyncio
async def test_no_shipped_doctrine_routes_setup_to_an_agent():
    """The second runner is gone from the prompts too. A stale recipe branch
    would put the decision back where it cannot be made correctly."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "casa/rootfs/opt/casa/defaults"
    banned = ("run_plugin_setup_tool", "setup_via_consent", "consent_pending")
    offenders = sorted(
        f"{p.relative_to(root)}:{t}"
        for p in root.rglob("*.md")
        for t in banned
        if t in p.read_text(encoding="utf-8"))
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_the_dispatch_claims_no_approval_that_did_not_happen(env):
    """A zero-member verdict means nobody approved anything. Saying otherwise
    in the setup turn is the same invented fact as #443's "integration is
    dead" (INV-TOOL-005)."""
    env.plugin = _plugin()
    await _reconcile(env)
    text = env.dispatched[0][1]
    assert "operator approved" not in text
    assert "needed no new consent" in text
    # ...and the approved path still says so.
    env.plugin = _plugin(triggers=True, artifact="art-2")
    env.entry = dict(env.entry, artifact_id="art-2")
    await _reconcile(env)
    _approve_trigger(env)
    await _reconcile(env)
    assert "operator approved" in env.dispatched[1][1]


def test_asyncio_is_imported_for_the_module_contract():
    """Guard against the import drifting away — _reconcile awaits real work."""
    assert asyncio is not None



# ---------------------------------------------------------------------------
# Upgrade: a v3 store must not lose an already-approved setup run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_v3_pending_episode_still_dispatches_after_upgrade(env):
    """A v3 row existed ONLY because settlement had already released it (all
    members approved). It carries no `gate`, so treating a gate-less row as
    awaiting a verdict would hold it forever: the round that would release it
    is long consumed, and `ensure_obligation` declines to create a replacement
    while a pending row for that artifact exists. The approved-but-undispatched
    window would silently lose its automatic setup across the upgrade."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 3,
        "rounds": {},
        "consumed_keys": ["deadbeef"],
        "episodes": [{
            "id": "old1", "key": "deadbeef", "plugin": "gmail",
            "artifact_id": "art-1", "setup_tool": "setup_gmail",
            "approved_identities": ["i#g1"], "status": "pending",
            "attempts": 0, "created_ts": 1.0, "updated_ts": 1.0}],
    }), encoding="utf-8")
    await pse._worker_pass()
    assert len(env.dispatched) == 1
    row = _obligation()
    assert row["status"] == "dispatched"
    assert row["gen"] == 0
    assert "key" not in row and "setup_tool" not in row   # vestigial in v4
    assert pse._load()["schema_version"] == 4
    assert "consumed_keys" not in pse._load()


@pytest.mark.asyncio
async def test_a_v3_refused_style_row_is_not_resurrected(env):
    """Migration must not turn a TERMINAL v3 row into a dispatchable one."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 3, "rounds": {}, "episodes": [{
            "id": "old2", "plugin": "gmail", "artifact_id": "art-1",
            "setup_tool": "setup_gmail", "status": "dispatched",
            "attempts": 1, "created_ts": 1.0, "updated_ts": 1.0}],
    }), encoding="utf-8")
    await pse._worker_pass()
    assert env.dispatched == []
    assert _obligation()["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Review round 1 findings (Sol + Terra)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_creates_the_obligation_it_owes(env):
    """Both reviewers, independently: the sweep used to be gated on `prompt`,
    and BOTH boot reconcilers pass prompt=False. So a crash between a durable
    registry publish and its lifecycle reconcile left the obligation
    uncreated — no pending row, nothing in health, setup never run — which is
    exactly the recovery the level-triggered design claims to provide."""
    env.plugin = _plugin()                             # setupTool, no consent
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    # The boot pass, exactly as casa_core makes it: no channel, prompt=False.
    await tr.reconcile_plugin_triggers(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.trig_acks,
        secrets_dir=env.secrets_dir, prompt=False,
        resolver=_resolver, global_secret_ok=lambda: True)
    await cr.reconcile_plugin_callbacks(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.cb_acks, spool=_Spool(),
        resolver=_resolver, entries=_entries, prompt=False)
    assert _obligation() is not None, "boot owes this plugin a setup run"
    await pse._worker_pass()
    assert len(env.dispatched) == 1


@pytest.mark.asyncio
async def test_a_late_denial_cannot_revoke_a_release(env):
    """Sol: the nonce fence protects a LIVE member, but a late deny/expiry
    whose round is already consumed synthesizes a fresh round in which the
    member is absent — so the fence is skipped by construction and the denial
    used to refuse an obligation a settled round had already released. Nothing
    then re-arms it: the ack exists, so no re-prompt ever comes."""
    # The window that matters is RELEASED BUT NOT YET DISPATCHED — Sol's
    # scenario holds it on an unresolved environment variable. A row that
    # already dispatched is out of `pending` and was never at risk.
    pse.configure(
        dispatch=lambda r, i, c: _unreachable(),
        notify_operator=_swallow(env), resolve_registry_entry=lambda p: env.entry,
        ack_lookup=lambda i: None, routes_live=lambda p: True,
        secrets_ready=lambda p: False)                  # held here
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    ident = _trigger_identity(env)
    _approve_trigger(env)
    await _reconcile(env)
    assert env.dispatched == []                        # held, not dispatched
    row = _obligation()
    assert row["status"] == "pending" and row["gate"] == "released"
    assert pse._load()["rounds"] == {}                 # round consumed
    # The superseded keyboard's expiry finally lands, with no member to fence.
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity=ident, approved=False, nonce="dead")
    row = _obligation()
    assert row["status"] != "refused", "a late denial revoked an earned release"
    assert row["gate"] == "released"
    # ...and once the environment resolves, setup still runs.
    pse.configure(
        dispatch=_recording(env), notify_operator=_swallow(env),
        resolve_registry_entry=lambda p: env.entry,
        ack_lookup=lambda i: None, routes_live=lambda p: True,
        secrets_ready=lambda p: True)
    await pse._worker_pass()
    assert len(env.dispatched) == 1


# ---------------------------------------------------------------------------
# Review round 2 findings (Sol + Terra converged on all three)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_after_a_denial_and_a_restart_still_runs_setup(env):
    """The denial note promises that approving the consent will run setup. This
    pins that end to end, across a promptless pass in between.

    Both round-2 reviewers predicted this was BROKEN, from opposite directions,
    because re-arming was driven by whether `open_round` minted a fresh nonce —
    a fact about prompting, not about consent. Their sequences do not actually
    reach it: a row only becomes terminal by settling or dispatching, both of
    which consume the round, so `open_round` always saw an absent member and
    always minted. Re-arming now reads the reconciler's pending set directly,
    which is the condition it always meant; the proxy happened to agree only
    because of an unstated "terminal row implies no live round" invariant."""
    env.plugin = _plugin(triggers=True)
    await _reconcile(env)
    ident = _trigger_identity(env)
    await pse.on_consent_decision(plugin="gmail", artifact_id="art-1",
                                  identity=ident, approved=False, nonce="")
    assert _obligation()["status"] == "refused"
    # A promptless pass — boot after a restart, or any prompt=False reload.
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[env.plugin],
                               issues=[])

    await tr.reconcile_plugin_triggers(
        trigger_registry=env.registry, role_configs=role_configs,
        channel_manager=None, acks=env.trig_acks,
        secrets_dir=env.secrets_dir, prompt=False,
        resolver=_resolver, global_secret_ok=lambda: True)
    # ...then the operator is re-prompted and approves.
    await _reconcile(env)
    _approve_trigger(env)
    await _reconcile(env)
    assert len(env.dispatched) == 1, "approving after a denial must run setup"


@pytest.mark.asyncio
async def test_a_malformed_row_does_not_strand_every_plugin(env):
    """Sol + Terra: the store guard only checks that `episodes` is a LIST, so
    one non-dict element parsed fine and then raised on the first `e.get(...)`
    in `_row_for` / `episodes()` / `health_issues()`. That stranded EVERY
    plugin's setup and broke health regeneration, and the "a corrupt store must
    not brick boot" recovery never applied because the store was not corrupt at
    the level it checks."""
    import json
    pse.STORE_PATH.write_text(json.dumps({
        "schema_version": 4, "rounds": {},
        "episodes": [None, "partial write", 7],
    }), encoding="utf-8")
    assert pse.episodes() == []
    assert pse.health_issues() == []
    env.plugin = _plugin()
    await _reconcile(env)
    assert len(env.dispatched) == 1
    assert _obligation() is not None
