"""#453 + #454 — a reconcile pass must describe ONE registry, and the setup
gate must read what the pass APPLIED.

Both defects predate v0.161.0 and were recorded during its adversarial review.

* **#454** — ``compute_desired`` resolves the registry once per target, each
  read hitting the live snapshot. A ``reload_snapshot`` landing between two
  reads yields a pass that composes generation A's manifests with generation
  B's assignment authority, and the overlay it swaps in can publish a route
  generation B removed. The callback reconciler additionally read assignment
  through ``entries()``, which was not resolver-pinned at all.
* **#453** — the setup-dispatch gate (``casa_core._callback_and_trigger_routes_live``
  → each reconciler's ``current_issues``) is derived from ``compute_desired``,
  which knows only about consent, assignment and declarations. The artifacts a
  plugin's setup tool actually reads — the per-trigger webhook secret and the
  callback ``ready.json``/index pair — are produced by the reconcile's APPLY
  half. Nothing re-read them, so the gate reported "live" while the secret was
  absent (first approval), bound to the PREVIOUS approval generation (a
  re-approval after a revoke), or lazily minted by the webhook handler and
  about to be replaced.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import callback_reconcile as cr
import plugin_registry
import trigger_reconcile as tr
import webhook_auth
from plugin_fixtures import entry, mk_artifact, mk_registry

TRIGGER_AUTH = {"mode": "static_header", "header": "X-API-Key"}
EFFECTIVE = "plg-gmail--push"


# ---------------------------------------------------------------------------
# #454 — one registry generation per pass
# ---------------------------------------------------------------------------


def _empty(tmp_path: Path, sub: str) -> Path:
    """An empty registry file, published as its own directory."""
    d = Path(tmp_path) / sub
    d.mkdir(parents=True, exist_ok=True)
    return mk_registry(d, [])


def _publish(tmp_path: Path, name: str, targets: list[str]) -> Path:
    """Publish a one-plugin registry + store, and make it the live snapshot."""
    store = tmp_path / "store"
    e = entry(name, targets)
    mk_artifact(store, name, e["artifact_id"])
    plugin_registry.reload_snapshot(
        registry_path=mk_registry(tmp_path, [e]), store_root=store)
    return store


def test_a_pinned_resolver_serves_every_target_from_one_snapshot(tmp_path):
    """The fix #454 asks for: ONE resolution for the whole pass. A reload
    landing mid-pass must not be visible to the second target — composing two
    generations is exactly what publishes a route the newer one removed."""
    _publish(tmp_path, "gmail", ["resident:assistant"])
    resolve = plugin_registry.pinned_resolver()
    before = resolve(None)
    assert [p.name for p in before.plugins] == ["gmail"]

    # A concurrent reload removes the plugin outright.
    plugin_registry.reload_snapshot(
        registry_path=_empty(tmp_path, "empty"), store_root=tmp_path / "store")

    after = resolve("resident:assistant")
    assert after.generation == before.generation, "the pass moved generation"
    assert [p.name for p in after.plugins] == ["gmail"], (
        "assignment authority came from a different registry than the manifests")
    assert resolve.generation == before.generation


def test_a_pinned_resolver_serves_entries_from_the_same_snapshot(tmp_path):
    """The callback reconciler's assignment authority is ``entries()``, a
    further independent read. It must ride the same pin."""
    _publish(tmp_path, "gmail", ["resident:assistant"])
    resolve = plugin_registry.pinned_resolver()
    assert [e["name"] for e in resolve.entries()] == ["gmail"]

    plugin_registry.reload_snapshot(
        registry_path=_empty(tmp_path, "empty2"), store_root=tmp_path / "store")

    assert [e["name"] for e in resolve.entries()] == ["gmail"]
    assert plugin_registry.snapshot_registry().entries == [], (
        "the live snapshot did change — the pin is what held")


@pytest.mark.parametrize("module", [tr, cr])
def test_each_reconcilers_default_resolver_is_pinned(tmp_path, module):
    """Every pass builds its resolver through the module default, so pinning
    there is what makes ``one_generation`` structurally true rather than a
    property the pass merely hopes for."""
    _publish(tmp_path, "gmail", ["resident:assistant"])
    resolve = module._default_resolver()
    first = resolve(None)
    plugin_registry.reload_snapshot(
        registry_path=_empty(tmp_path, f"e-{module.__name__}"),
        store_root=tmp_path / "store")
    second = resolve("resident:assistant")
    assert first.generation == second.generation
    assert [p.name for p in second.plugins] == ["gmail"]


def test_a_reconcile_pass_never_composes_two_generations(tmp_path):
    """End to end through the real compute: the overlay a pass swaps in is
    derived from one registry. A reload between the manifest read and the
    assignment read used to let the pass publish a route the newer generation
    had removed."""
    _publish(tmp_path, "gmail", ["resident:assistant"])
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    pinned = tr.pin_resolver(tr._default_resolver())
    pinned(None)
    # The reload lands mid-pass, between the two resolver reads.
    plugin_registry.reload_snapshot(
        registry_path=_empty(tmp_path, "gone"), store_root=tmp_path / "store")
    pinned("resident:assistant")
    assert tr.one_generation(pinned) is True, (
        "a pinned pass must not span registry generations")


def test_an_unpinned_drifting_resolver_still_refuses_to_conclude():
    """The generation guard stays as the safety net for any pass that does NOT
    come through the pinned default (an injected test seam, a future caller)."""
    gens = iter([1, 2])

    def _drifting(target):
        return SimpleNamespace(registry_valid=True, plugins=[], issues=[],
                               generation=next(gens))

    pinned = tr.pin_resolver(_drifting)
    pinned(None)
    pinned("resident:assistant")
    assert tr.one_generation(pinned) is False


# ---------------------------------------------------------------------------
# #453 — the gate reads the APPLIED artifacts, not the derived intent
# ---------------------------------------------------------------------------


def _trigger_plugin(artifact="art-1"):
    return SimpleNamespace(
        name="gmail", artifact_id=artifact, path=f"/store/gmail/{artifact}",
        version="1.0.0", manifest_name="gmail",
        manifest={"name": "gmail", "casa": {"triggers": [
            {"name": "push", "type": "webhook", "target": "resident:assistant",
             "auth": dict(TRIGGER_AUTH)}]}})


def _acks(tmp_path, name="trigger_acks.json"):
    from trigger_acks import TriggerAckStore

    return TriggerAckStore(path=Path(tmp_path) / name)


def _approve(acks, plugin):
    """Record the consent the reconciler looks for. The pending row's ``auth``
    is the NORMALIZED map, so the identity has to come from the compute's own
    row — a hand-built one hashes differently and never matches."""
    row = tr.compute_desired(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=SimpleNamespace(get=lambda ident: None),
        resolver=lambda t: SimpleNamespace(
            registry_valid=True, plugins=[plugin], issues=[]),
        global_secret_ok=lambda: True).pending[0]
    acks.record(identity=tr.ack_identity(
        plugin=row["plugin"], artifact_id=row["artifact_id"],
        effective=row["effective"], target=row["target"], auth=row["auth"]),
        plugin=row["plugin"], artifact_id=row["artifact_id"],
        effective=row["effective"], target=row["target"], auth=row["auth"])
    return acks


def _acked(tmp_path, plugin, name="trigger_acks.json"):
    return _approve(_acks(tmp_path, name), plugin)


def _issue_codes(desired):
    return sorted(str(i.reason_code) for i in desired.issues)


def _desired(plugin, acks):
    return tr.compute_desired(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=acks, resolver=lambda t: SimpleNamespace(
            registry_valid=True, plugins=[plugin], issues=[]),
        global_secret_ok=lambda: True)


def test_an_unminted_per_trigger_secret_is_a_gap(tmp_path):
    """The first-approval window. The ack persists and the round settles in one
    yield-free step; the mint happens in the reconcile that follows. Between
    them the derived compute sees a fully-consented trigger and reported no gap
    at all, so the setup tool could be dispatched to provision an external
    service against a secret that did not exist yet."""
    plugin = _trigger_plugin()
    acks = _acked(tmp_path, plugin)
    desired = _desired(plugin, acks)
    assert desired.overlay, "the consent is complete — the route is desired"
    assert _issue_codes(desired) == []

    tr.verify_minted_secrets(desired, tmp_path / "secrets")
    assert _issue_codes(desired) == ["trigger_secret_missing"]
    assert desired.overlay == {}, "an unbacked route is not routable"


def test_a_secret_bound_to_the_previous_approval_is_a_gap(tmp_path):
    """The rotation window #453 names. A revoke then a re-approval mints under
    a NEW (identity, approval generation) pair, but the pre-rotation file is
    still on disk and the derived compute reports no issue — so the gate opened
    and setup could point the provider at a credential Casa was about to
    replace."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    plugin = _trigger_plugin()
    acks = _acked(tmp_path, plugin)

    first = _desired(plugin, acks)
    tr._mint_secrets(first, secrets)
    assert _issue_codes(first) == [] and first.overlay

    # The operator revokes and re-approves: the same consent tuple, a NEW
    # approval generation — which is exactly what the mint rekeys on.
    acks.revoke_plugin("gmail")
    _approve(acks, plugin)
    second = _desired(plugin, acks)
    assert (next(iter(second.overlay.values()))["identity"]
            != next(iter(first.overlay.values()))["identity"])
    tr.verify_minted_secrets(second, secrets)
    assert _issue_codes(second) == ["trigger_secret_missing"]
    assert second.overlay == {}

    # The reconcile's mint rekeys the file, and the gap closes.
    third = _desired(plugin, acks)
    tr._mint_secrets(third, secrets)
    tr.verify_minted_secrets(third, secrets)
    assert _issue_codes(third) == []
    assert third.overlay


def test_a_handler_minted_unbound_secret_is_a_gap(tmp_path):
    """The webhook handler mints lazily for an unrouted name, with no identity
    binding. ``ensure_secret_for_identity`` retires and re-mints such a secret
    at the next reconcile — so a setup tool that read the file first provisioned
    a credential the reconcile then replaced."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    assert webhook_auth.ensure_secret(
        EFFECTIVE, owner="casa", secrets_dir=secrets)   # the lazy handler mint

    plugin = _trigger_plugin()
    desired = _desired(plugin, _acked(tmp_path, plugin))
    tr.verify_minted_secrets(desired, secrets)
    assert _issue_codes(desired) == ["trigger_secret_missing"]


def test_a_global_secret_trigger_needs_no_per_trigger_file(tmp_path):
    """``hmac_body`` is backed by the GLOBAL secret, which ``compute_desired``
    already gates on. The verification must not invent a gap for it."""
    plugin = _trigger_plugin()
    plugin.manifest["casa"]["triggers"][0]["auth"] = {"mode": "hmac_body"}
    desired = _desired(plugin, _acked(tmp_path, plugin))
    tr.verify_minted_secrets(desired, tmp_path / "secrets")
    assert _issue_codes(desired) == []
    assert desired.overlay


def test_current_issues_reports_the_unbacked_route(tmp_path, monkeypatch):
    """The gate's actual entry point. ``_callback_and_trigger_routes_live``
    reads ``current_issues()``, so the verification has to be folded in there —
    it is the ONE recomputation both the health report and the dispatch gate
    consume."""
    import agent as agent_mod

    plugin = _trigger_plugin()
    acks = _acked(tmp_path, plugin)
    monkeypatch.setattr(tr, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(tr, "_default_acks", lambda: acks)
    monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])}),
        raising=False)
    monkeypatch.setattr(tr, "_default_resolver", lambda: (
        lambda t: SimpleNamespace(registry_valid=True, plugins=[plugin],
                                  issues=[])))
    monkeypatch.setattr(tr, "_default_global_secret_ok", lambda: (lambda: True))

    codes = [str(i.reason_code) for i in tr.current_issues()]
    assert codes == ["trigger_secret_missing"]


# ---------------------------------------------------------------------------
# #453, callback half — the marker pair is the artifact a setup tool reads
# ---------------------------------------------------------------------------


class _Spool:
    """The durable marker inventory (the shape ``callback_reconcile`` reads
    back and compares byte-strictly)."""

    def __init__(self):
        self._ready: dict = {}
        self._index: dict = {}

    def ensure_plugin_dirs(self, plugin): pass

    def write_ready(self, plugin, payload):
        self._ready[plugin] = payload

    def delete_ready(self, plugin):
        self._ready.pop(plugin, None)
        return True

    def write_index_entry(self, path, payload):
        import callback_spool
        self._index[callback_spool.index_key(path)] = payload

    def delete_index_entry(self, path):
        import callback_spool
        self._index.pop(callback_spool.index_key(path), None)
        return True

    def delete_index_key(self, key):
        self._index.pop(key, None)
        return True

    def published_plugins(self):
        return sorted(self._ready)

    def index_keys(self):
        return sorted(self._index)

    @staticmethod
    def _marker(payload):
        import callback_spool
        if payload is None:
            return callback_spool.Marker(callback_spool.MarkerState.ABSENT)
        return callback_spool.Marker(
            callback_spool.MarkerState.PRESENT, payload,
            raw=callback_spool.canonical_marker_bytes(payload))

    def read_marker(self, plugin):
        return self._marker(self._ready.get(plugin))

    def read_index_marker(self, path):
        import callback_spool
        return self._marker(self._index.get(callback_spool.index_key(path)))


def _callback_plugin(artifact="art-1"):
    return SimpleNamespace(
        name="gmail", artifact_id=artifact, path=f"/store/gmail/{artifact}",
        version="1.0.0", manifest_name="gmail",
        manifest={"name": "gmail",
                  "casa": {"callbacks": [{"name": "authorize"}]}})


def _cb_desired(tmp_path, plugin, monkeypatch):
    from callback_acks import CallbackAckStore
    from plugin_callbacks import declaration_digest

    monkeypatch.setattr(cr, "_base_url", lambda: "https://casa.example.org")
    acks = CallbackAckStore(path=tmp_path / "callback_acks.json")
    acks.record("gmail", "plg-gmail--authorize", declaration_digest(
        {"declared": "authorize", "effective": "plg-gmail--authorize"}))
    return cr.compute_desired(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        acks=acks,
        resolver=lambda t: SimpleNamespace(registry_valid=True,
                                           plugins=[plugin], issues=[]),
        entries=lambda: [{"name": "gmail", "artifact_id": plugin.artifact_id,
                          "targets": ["resident:assistant"]}])


def test_an_unpublished_callback_marker_is_a_gap(tmp_path, monkeypatch):
    """The callback ordering #453 names last. The consent settles the round
    before ``_publish_markers_post_swap`` writes the pair, and the setup tool
    reads its redirect URI out of that pair — so the gate must not open until
    the pair on disk equals the desired one."""
    plugin = _callback_plugin()
    desired = _cb_desired(tmp_path, plugin, monkeypatch)
    assert desired.routed and desired.issues == []

    spool = _Spool()
    cr.verify_published_markers(desired, spool)
    assert [str(i.reason_code) for i in desired.issues] == [
        "callback_spool_error"]

    # After the reconcile's post-swap publish, the pair matches and it clears.
    fresh = _cb_desired(tmp_path, plugin, monkeypatch)
    cr._publish_markers_post_swap(spool, fresh, fresh.routed)
    checked = _cb_desired(tmp_path, plugin, monkeypatch)
    cr.verify_published_markers(checked, spool)
    assert checked.issues == []


def test_a_stale_callback_marker_is_a_gap(tmp_path, monkeypatch):
    """A marker left over from a PREVIOUS artifact advertises the wrong
    redirect URI. Byte-strict, exactly as the reconcile's own pair compare."""
    old = _callback_plugin()
    spool = _Spool()
    published = _cb_desired(tmp_path, old, monkeypatch)
    cr._publish_markers_post_swap(spool, published, published.routed)

    new = _callback_plugin(artifact="art-2")
    desired = _cb_desired(tmp_path, new, monkeypatch)
    cr.verify_published_markers(desired, spool)
    assert [str(i.reason_code) for i in desired.issues] == [
        "callback_spool_error"]


def test_an_unwired_spool_is_a_gap(tmp_path, monkeypatch):
    """No spool means nothing can be published for the consumer to read — the
    reconcile already surfaces that per plugin, and the gate must agree."""
    desired = _cb_desired(tmp_path, _callback_plugin(), monkeypatch)
    cr.verify_published_markers(desired, None)
    assert [str(i.reason_code) for i in desired.issues] == [
        "callback_spool_error"]


@pytest.mark.asyncio
async def test_setup_holds_until_the_secret_it_needs_exists(tmp_path,
                                                           monkeypatch):
    """INV-PLUG-011 end to end, through the REAL gate casa_core wires into the
    episode worker — the acceptance matrix in
    ``tests/test_plugin_setup_single_runner.py`` doubles ``routes_live``, so the
    claim that the gate itself holds has to be pinned here.

    The sequence is the production one: the operator approves, the ack persists,
    the round settles and kicks the worker — and the mint happens in the
    reconcile the finish hook awaits afterwards. The worker waking in that window
    must find a gap, not a green route."""
    import agent as agent_mod
    import casa_core
    import plugin_setup_episodes as pse

    plugin = _trigger_plugin()
    acks = _acked(tmp_path, plugin)
    secrets = tmp_path / "webhook_secrets"
    dispatched: list = []

    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)
    monkeypatch.setattr(tr, "SECRETS_DIR", secrets)
    monkeypatch.setattr(tr, "_default_acks", lambda: acks)
    monkeypatch.setattr(tr, "_default_resolver", lambda: (
        lambda t=None: SimpleNamespace(registry_valid=True, plugins=[plugin],
                                       issues=[])))
    monkeypatch.setattr(tr, "_default_global_secret_ok", lambda: (lambda: True))
    # No callbacks are declared, so the callback half of the gate is silent.
    monkeypatch.setattr(cr, "_default_resolver", lambda: (
        lambda t=None: SimpleNamespace(registry_valid=True, plugins=[plugin],
                                       issues=[])))
    monkeypatch.setattr(cr, "_default_entries", lambda: (lambda: []))
    # The gate supplies ONE pinned resolution to both halves (#454), so the
    # fake registry has to be injected there — patching the per-module defaults
    # alone would leave the gate resolving the real, empty snapshot and
    # reporting every plugin live for want of anything to see.
    import plugin_registry as _pr

    def _fake_pin():
        def _resolve(target=None):
            return SimpleNamespace(registry_valid=True, plugins=[plugin],
                                   issues=[], generation=1)
        _resolve.generation = 1
        _resolve.entries = lambda: [
            {"name": "gmail", "artifact_id": "art-1",
             "targets": ["resident:assistant"]}]
        return _resolve

    monkeypatch.setattr(_pr, "pinned_resolver", _fake_pin)
    monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])}),
        raising=False)

    async def _dispatch(role, instruction, ctx):
        dispatched.append(role)
        return True

    async def _notify(text):
        pass

    pse.configure(
        dispatch=_dispatch, notify_operator=_notify,
        resolve_registry_entry=lambda p: {
            "artifact_id": "art-1", "setup_tool": "setup_gmail",
            "granted_tools": ["gmailsrv"], "targets": ["resident:assistant"]},
        ack_lookup=lambda ident: None,
        routes_live=casa_core._callback_and_trigger_routes_live)

    # The approval has landed and the round released the obligation, but the
    # reconcile that mints has not run yet.
    pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=[])
    await pse._recover_and_settle()
    assert pse.episodes()[0]["gate"] == "released"

    await pse._worker_pass()
    assert dispatched == [], "setup ran before its secret existed"
    assert pse.episodes()[0]["last_error"] == "waiting for live trigger route"

    # The reconcile mints, and the same held obligation lands.
    secrets.mkdir(exist_ok=True)
    desired = _desired(plugin, acks)
    tr._mint_secrets(desired, secrets)
    await pse._worker_pass()
    assert dispatched == ["assistant"]


# ---------------------------------------------------------------------------
# Review round 1 (Sol F1 / Terra P1, converged) — the hold must be escapable
# ---------------------------------------------------------------------------


async def _callback_reconcile(plugin, spool, acks, *, clean=True):
    """One real callback reconcile. ``clean=False`` gives the resolution an
    issue for some OTHER plugin, which is what makes the pass untrustworthy."""
    from plugin_registry import PluginIssue
    from trigger_registry import TriggerRegistry

    issues = [] if clean else [PluginIssue(
        name="broken", target=None, stage="resolve",
        reason_code="artifact_missing", artifact_id="art-x")]
    await cr.reconcile_plugin_callbacks(
        trigger_registry=TriggerRegistry(scheduler=None, app=None, bus=None),
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        channel_manager=None, acks=acks, spool=spool,
        resolver=lambda t: SimpleNamespace(registry_valid=True,
                                           plugins=[plugin], issues=issues),
        entries=lambda: [{"name": "gmail",
                          "artifact_id": plugin.artifact_id,
                          "targets": ["resident:assistant"]}],
        prompt=False)


def _cb_acks(tmp_path):
    from callback_acks import CallbackAckStore
    from plugin_callbacks import declaration_digest

    acks = CallbackAckStore(path=tmp_path / "callback_acks.json")
    acks.record("gmail", "plg-gmail--authorize", declaration_digest(
        {"declared": "authorize", "effective": "plg-gmail--authorize"}))
    return acks


@pytest.mark.asyncio
async def test_a_routed_pair_is_republished_even_on_an_untrustworthy_pass(
        tmp_path, monkeypatch):
    """Both reviewers, independently, on the first release of this gate: the
    marker WRITER declined to rewrite an existing-but-different pair whenever
    the pass was untrustworthy, and `prunable` is registry-GLOBAL — one
    unresolvable plugin anywhere makes every pass untrustworthy for as long as
    it stays broken. The new reader had no such condition, so it demanded an
    artifact the writer had decided never to write, and the setup obligation of
    every routed plugin held forever with no operator action on that plugin
    able to clear it.

    Sol's trigger is the ordinary one: a plugin UPDATE changes the artifact
    path, so the index key moves and its entry goes absent while `ready.json`
    stays byte-identical (the payload carries no artifact id) — the documented
    "consent survives a routine upgrade" path.

    The availability double-gate exists to stop a bad compute DELETING a live
    consumer's marker. A plugin in the routed set resolved cleanly in this very
    pass and holds a persisted ack, so rewriting ITS pair from ITS own
    resolution destroys nothing — which is why the gate belongs on the orphan
    retirement (asserted below) and not here."""
    monkeypatch.setattr(cr, "_base_url", lambda: "https://casa.example.org")
    acks = _cb_acks(tmp_path)
    spool = _Spool()

    await _callback_reconcile(_callback_plugin(), spool, acks)
    assert spool.published_plugins() == ["gmail"]
    first_keys = spool.index_keys()

    # The update: same declaration (the ack survives), new artifact path.
    updated = _callback_plugin(artifact="art-2")
    await _callback_reconcile(updated, spool, acks, clean=False)

    assert spool.index_keys() != first_keys, (
        "the moved index entry was never republished")
    desired = _cb_desired(tmp_path, updated, monkeypatch)
    cr.verify_published_markers(desired, spool)
    assert desired.issues == [], "the setup gate has no way out of this state"


@pytest.mark.asyncio
async def test_an_untrustworthy_pass_still_never_retires_an_orphan(
        tmp_path, monkeypatch):
    """The other half of that condition, which must NOT move: a plugin absent
    from an untrustworthy pass's routed set may simply have failed to resolve,
    and deleting its marker on that basis is the data loss the double-gate was
    added to prevent."""
    monkeypatch.setattr(cr, "_base_url", lambda: "https://casa.example.org")
    acks = _cb_acks(tmp_path)
    spool = _Spool()
    await _callback_reconcile(_callback_plugin(), spool, acks)
    assert spool.published_plugins() == ["gmail"]

    # An untrustworthy pass in which gmail does not resolve at all.
    from plugin_registry import PluginIssue
    from trigger_registry import TriggerRegistry
    await cr.reconcile_plugin_callbacks(
        trigger_registry=TriggerRegistry(scheduler=None, app=None, bus=None),
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])},
        channel_manager=None, acks=acks, spool=spool,
        resolver=lambda t: SimpleNamespace(
            registry_valid=True, plugins=[],
            issues=[PluginIssue(name="gmail", target=None, stage="resolve",
                                reason_code="artifact_missing",
                                artifact_id="art-1")]),
        entries=lambda: [], prompt=False)
    assert spool.published_plugins() == ["gmail"], (
        "a resolution hiccup deleted a live consumer's marker")


@pytest.mark.asyncio
async def test_an_update_during_the_route_check_never_runs_the_old_setup_tool(
        tmp_path, monkeypatch):
    """Sol, on the re-review, against a fix Sol itself asked for: moving the
    route gate off the event loop inserted the FIRST yield between the
    supersession check and the dispatch. That window used to be structurally
    empty — every gate between them is synchronous — which is what made "a
    superseded artifact must never fire" true rather than merely likely.

    A `plugin_update` completing inside it left the episode dispatching the OLD
    artifact's setup tool from a captured entry, against the provider. Nothing
    downstream catches it: the resident's published binding still names the old
    artifact until a reload, and a specialist target has no binding check at
    all."""
    import plugin_setup_episodes as pse

    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)
    dispatched: list = []
    entry = {"artifact_id": "art-1", "setup_tool": "setup_gmail_v1",
             "granted_tools": ["gmailsrv"], "targets": ["resident:assistant"]}

    def _routes_live(plugin):
        # The update lands while this gate is awaited off the loop.
        entry["artifact_id"] = "art-2"
        entry["setup_tool"] = "setup_gmail_v2"
        return True

    async def _dispatch(role, instruction, ctx):
        dispatched.append(instruction)
        return True

    async def _notify(text):
        pass

    pse.configure(
        dispatch=_dispatch, notify_operator=_notify,
        resolve_registry_entry=lambda p: dict(entry),
        ack_lookup=lambda i: None, routes_live=_routes_live)

    pse.ensure_obligation(plugin="gmail", artifact_id="art-1")
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=[])
    await pse._recover_and_settle()
    assert pse.episodes()[0]["gate"] == "released"

    await pse._worker_pass()
    assert dispatched == [], "dispatched a superseded artifact's setup tool"
    row = pse.episodes()[0]
    assert row["status"] == "pending", row
    # The next kick re-runs the ladder from the top, where the three-state
    # resolution reaches the terminal supersession verdict properly.
    await pse._worker_pass()
    assert dispatched == []
    assert pse.episodes()[0]["status"] == "stale"


@pytest.mark.parametrize("half", ["trigger", "callback"])
def test_a_recomputation_that_could_not_run_keeps_the_plugin_dark(monkeypatch,
                                                                  half):
    """Terra: an empty issue list is the POSITIVE claim "this plugin has no
    gap", and it is exactly what opens the gate — so degrading a crash to `[]`
    turned the one check that must fail closed into one that fails open. The
    same held before the runtime is up, where both halves also returned `[]`.
    Each half now reports whether it could compute at all."""
    import casa_core

    def _boom(*a, **kw):
        raise RuntimeError("compute exploded")

    monkeypatch.setattr(tr if half == "trigger" else cr, "compute_desired",
                        _boom)
    monkeypatch.setattr(
        tr if half == "trigger" else cr, "_default_acks", lambda: None)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(
        role_configs={"assistant": SimpleNamespace(channels=["webhook"])}),
        raising=False)

    assert casa_core._callback_and_trigger_routes_live("gmail") is False


def test_no_runtime_yet_keeps_the_plugin_dark(monkeypatch):
    """The same rule for the pre-boot window: "I cannot evaluate this" is not
    "there is nothing wrong"."""
    import agent as agent_mod
    import casa_core

    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    assert casa_core._callback_and_trigger_routes_live("gmail") is False
    # ...while HEALTH still degrades to no extras, where a missing row hides a
    # problem but an invented one cries wolf.
    assert tr.current_issues() == [] and cr.current_issues() == []

