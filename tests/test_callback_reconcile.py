"""The authorization-callback reconciler.

``callback_reconcile`` is the ONE writer of the TriggerRegistry *callback*
overlay: it derives the COMPLETE desired overlay (every resolved + assigned +
validly-declared + acked plugin callback), swaps it atomically, maintains the
spool's ``ready.json`` / ``.index`` files with the asymmetric ordering that
keeps the readiness marker from ever being falsely positive, prunes stale acks
and fires consent prompts for callbacks whose ONLY gap is the ack.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import callback_reconcile as cr
import callback_spool
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity, declaration_digest
from trigger_registry import TriggerRegistry

BASE = "https://casa.example.org"
# Captured before any test patches the seam (the autouse fixture below pins a
# deterministic base for every other test).
_REAL_BASE_URL = cr._base_url


# ---------------------------------------------------------------------------
# fixtures / doubles
# ---------------------------------------------------------------------------


def _manifest(names):
    return {"name": "x", "casa": {"callbacks": [{"name": n} for n in names]}}


def _plugin(name="gmail", artifact_id="art-1", callbacks=("authorize",),
            path=None, manifest=None):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id,
        path=path if path is not None else f"/store/{name}/{artifact_id}",
        version="1.0.0", manifest_name=name,
        manifest=_manifest(callbacks) if manifest is None else manifest)


def _resolver(plugins, *, valid=True, issues=()):
    def resolve(target):
        return SimpleNamespace(registry_valid=valid, plugins=list(plugins),
                               issues=list(issues))
    return resolve


def _entries(*plugins, targets=("resident:assistant",)):
    rows = [{"name": p.name, "artifact_id": p.artifact_id,
             "targets": list(targets)} for p in plugins]

    def provider():
        return rows
    return provider


def _role_configs(**roles):
    return {role: SimpleNamespace(channels=list(channels))
            for role, channels in roles.items()}


def _ack(acks, plugin="gmail", declared="authorize"):
    effective = f"plg-{plugin}--{declared}"
    digest = declaration_digest({"declared": declared, "effective": effective})
    acks.record(plugin=plugin, effective=effective, declaration_digest=digest)
    return ack_identity(plugin, effective, digest)


def _identity(plugin="gmail", declared="authorize"):
    effective = f"plg-{plugin}--{declared}"
    return ack_identity(plugin, effective,
                        declaration_digest({"declared": declared,
                                            "effective": effective}))


class _SpoolStub:
    """Records the ordered file-side call sequence (the ordering contract)."""

    def __init__(self, calls, *, fail=()):
        self.calls = calls
        self.fail = set(fail)

    def _rec(self, what, *args):
        self.calls.append((what, *args))
        if what in self.fail:
            raise OSError("synthetic spool failure")

    def ensure_plugin_dirs(self, plugin):
        self._rec("ensure", plugin)

    def write_ready(self, plugin, payload):
        self._rec("ready", plugin, payload)

    def delete_ready(self, plugin):
        self._rec("del_ready", plugin)

    def write_index_entry(self, artifact_realpath, payload):
        self._rec("index", artifact_realpath, payload)

    def delete_index_entry(self, artifact_realpath):
        self._rec("del_index", artifact_realpath)

    # The durable-inventory reconcile reads these; a call-recording stub tracks
    # no on-disk state, so it reports an empty inventory (no orphan retirement).
    def published_plugins(self):
        return []

    def index_keys(self):
        return []

    # No on-disk payload either: an absent marker is never "stale" (so no
    # still-routed retirement) and always "needs write" (so the post-swap
    # rewrite still records ensure/ready/index, as these ordering tests pin).
    def read_ready(self, plugin):
        return None

    def read_index_entry(self, artifact_realpath):
        return None


class _SpyRegistry(TriggerRegistry):
    def __init__(self, calls=None):
        super().__init__(scheduler=None, app=None, bus=None)
        self._calls = calls if calls is not None else []

    def replace_callback_overlay(self, overlay):
        self._calls.append(("swap", dict(overlay)))
        super().replace_callback_overlay(overlay)


class _FakeTelegram:
    chat_id = "100"

    def __init__(self):
        self.posts = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        return True


class _FakeChannelManager:
    def __init__(self, telegram=None):
        self._telegram = telegram

    def get(self, name):
        return self._telegram if name == "telegram" else None


async def _reconcile(registry, *, plugins, acks, spool, entries=None,
                     role_configs=None, prompt=False, channel_manager=None,
                     resolver=None, base_url=BASE, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(cr, "_base_url", lambda: base_url)
    return await cr.reconcile_plugin_callbacks(
        trigger_registry=registry,
        role_configs=role_configs or _role_configs(assistant=["telegram"]),
        channel_manager=channel_manager, acks=acks, spool=spool,
        resolver=resolver or _resolver(plugins),
        entries=entries or _entries(*plugins), prompt=prompt)


@pytest.fixture(autouse=True)
def _pinned_base(monkeypatch):
    """Every test states its own base explicitly; never read the real env."""
    monkeypatch.setattr(cr, "_base_url", lambda: BASE)


# ---------------------------------------------------------------------------
# the gate matrix
# ---------------------------------------------------------------------------


async def test_valid_assigned_acked_callback_routes(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    calls: list = []
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub(calls))
    assert issues == []
    entry = registry.get_callback("plg-gmail--authorize")
    assert entry is not None
    assert entry["plugin"] == "gmail"
    assert entry["declared"] == "authorize"
    # The effective name is carried in the value (it is also the key) so the
    # callback handler reads the real value rather than a routed-name
    # fallback (callback_http._process).
    assert entry["effective"] == "plg-gmail--authorize"


async def test_invalid_declaration_darks_the_whole_set(tmp_path):
    """An intrinsically invalid declaration (reserved prefix) rejects the
    plugin's WHOLE callback set — the valid, acked sibling routes nothing."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin(callbacks=("authorize", "plg-sneaky"))
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_invalid"]
    assert issues[0].name == "gmail"
    assert issues[0].stage == "callbacks"
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_unassigned_plugin_is_callback_no_target(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              entries=_entries(p, targets=[]))
    assert [i.reason_code for i in issues] == ["callback_no_target"]
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_executor_only_assignment_is_no_target(tmp_path):
    """The delivery nudge targets a resident or a specialist; an
    executor-only plugin could never collect the code it accepted."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              entries=_entries(p, targets=["executor:cron"]))
    assert [i.reason_code for i in issues] == ["callback_no_target"]


async def test_unknown_resident_role_is_no_target(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              entries=_entries(p, targets=["resident:ghost"]))
    assert [i.reason_code for i in issues] == ["callback_no_target"]


async def test_specialist_assignment_is_a_valid_target(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(
        registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
        entries=_entries(p, targets=["specialist:finance"]))
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_unacked_callback_is_pending_and_dark(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_partial_ack_darks_the_whole_plugin(tmp_path):
    """All-or-nothing per plugin (INV-TRIG-003's callback mirror): one
    un-acked callback keeps the acked sibling dark too."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    p = _plugin(callbacks=("authorize", "renew"))
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]
    assert registry.get_callback("plg-gmail--authorize") is None
    assert registry.get_callback("plg-gmail--renew") is None


async def test_gate_order_no_target_outranks_pending_ack(tmp_path):
    """An unassigned plugin reports the ASSIGNMENT gap, never a consent
    prompt — approving a callback that still could not route is a broken
    promise (the non-consent gap suppresses the pending list)."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    desired = cr.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([p]), entries=_entries(p, targets=[]))
    assert [i.reason_code for i in desired.issues] == ["callback_no_target"]
    assert desired.pending == []


async def test_plugin_without_callbacks_is_silent(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin(manifest={"name": "x"})
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert issues == []


async def test_one_bad_plugin_never_darks_another(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    good = _plugin()
    bad = _plugin(name="badone", artifact_id="art-9",
                  callbacks=("plg-nope",))
    issues = await _reconcile(
        registry, plugins=[good, bad], acks=acks, spool=_SpoolStub([]),
        entries=_entries(good, bad))
    assert [(i.name, i.reason_code) for i in issues] == [
        ("badone", "callback_invalid")]
    assert registry.get_callback("plg-gmail--authorize") is not None


# ---------------------------------------------------------------------------
# the overlay swap
# ---------------------------------------------------------------------------


async def test_stale_overlay_entries_vanish_in_the_swap(tmp_path):
    registry = _SpyRegistry()
    registry.replace_callback_overlay({
        "plg-gone--old": {"plugin": "gone", "declared": "old",
                          "path": "/store/gone/art-0"}})
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]))
    assert registry.get_callback("plg-gone--old") is None
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_invalid_registry_fails_closed_to_an_empty_overlay(tmp_path):
    registry = _SpyRegistry()
    registry.replace_callback_overlay({
        "plg-gmail--authorize": {"plugin": "gmail", "declared": "authorize",
                                 "path": "/store/gmail/art-1"}})
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]),
                              resolver=_resolver([p], valid=False))
    assert issues == []          # the registry stage owns its own issues
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_compute_failure_fails_closed_and_propagates(tmp_path):
    registry = _SpyRegistry()
    registry.replace_callback_overlay({
        "plg-gmail--authorize": {"plugin": "gmail", "declared": "authorize",
                                 "path": "/store/gmail/art-1"}})
    acks = CallbackAckStore(path=tmp_path / "acks.json")

    def _boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError):
        await _reconcile(registry, plugins=[], acks=acks,
                         spool=_SpoolStub([]), resolver=_boom)
    assert registry.get_callback("plg-gmail--authorize") is None


# ---------------------------------------------------------------------------
# ready.json / .index — the asymmetric ordering
# ---------------------------------------------------------------------------


async def test_ready_and_index_written_only_after_the_swap(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=_SpoolStub(calls))
    kinds = [c[0] for c in calls]
    assert kinds == ["swap", "ensure", "ready", "index"]
    payload = calls[2][2]
    eff = "plg-gmail--authorize"
    assert payload == {
        "v": 1, "base_url": BASE,
        "callbacks": {"authorize": {
            "effective": eff,
            "redirect_uri": f"{BASE}/callback/{eff}"}}}
    assert calls[3][1] == p.path
    assert calls[3][2] == dict(payload, plugin_dir="gmail")


async def test_ready_and_index_deleted_before_the_unrouting_swap(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=_SpoolStub(calls))
    calls.clear()
    # the plugin disappears (uninstalled): marker + index die BEFORE the swap
    await _reconcile(registry, plugins=[], acks=acks, spool=_SpoolStub(calls),
                     entries=lambda: [])
    assert [c[0] for c in calls] == ["del_ready", "del_index", "swap"]
    assert calls[1][1] == p.path
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_revoked_ack_unroutes_marker_first(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=_SpoolStub(calls))
    acks.revoke_plugin("gmail")
    calls.clear()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=_SpoolStub(calls))
    assert [c[0] for c in calls][:3] == ["del_ready", "del_index", "swap"]
    assert registry.get_callback("plg-gmail--authorize") is None


async def test_artifact_change_retires_the_old_index_key_only(tmp_path):
    """The index is keyed by the RESOLVED artifact path: an update must drop
    the old key in the same pass that publishes the new one, and must not
    delete the (unchanged) plugin-dir readiness marker."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p1 = _plugin()
    await _reconcile(registry, plugins=[p1], acks=acks,
                     spool=_SpoolStub(calls))
    calls.clear()
    p2 = _plugin(artifact_id="art-2")
    await _reconcile(registry, plugins=[p2], acks=acks,
                     spool=_SpoolStub(calls))
    assert [c[0] for c in calls] == ["del_index", "swap", "ensure", "ready",
                                     "index"]
    assert calls[0][1] == p1.path
    assert calls[4][1] == p2.path


async def test_dropping_one_callback_retires_the_marker_before_the_swap(
    tmp_path,
):
    """'Never falsely positive' holds per FILE. A plugin that drops
    one of its callbacks would otherwise keep a ready.json advertising the
    dropped one across the swap window (and forever, if the rewrite fails)."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    _ack(acks, declared="renew")
    p2 = _plugin(callbacks=("authorize", "renew"))
    await _reconcile(registry, plugins=[p2], acks=acks, spool=_SpoolStub(calls))
    calls.clear()
    p1 = _plugin(callbacks=("authorize",))
    await _reconcile(registry, plugins=[p1], acks=acks, spool=_SpoolStub(calls))
    # BOTH published files carry the callbacks map, so both retire first
    assert [c[0] for c in calls] == ["del_ready", "del_index", "swap",
                                     "ensure", "ready", "index"]
    assert set(calls[4][2]["callbacks"]) == {"authorize"}
    assert registry.get_callback("plg-gmail--renew") is None


async def test_dropping_while_adding_retires_both_markers(tmp_path):
    """A strict-subset test misses the MIXED transition — drop one
    callback and add another in the same pass and the old marker still named
    the dropped one. Additions are irrelevant to the property."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    _ack(acks, declared="renew")
    await _reconcile(registry,
                     plugins=[_plugin(callbacks=("authorize", "renew"))],
                     acks=acks, spool=_SpoolStub(calls))
    calls.clear()
    _ack(acks, declared="refresh")
    await _reconcile(registry,
                     plugins=[_plugin(callbacks=("authorize", "refresh"))],
                     acks=acks, spool=_SpoolStub(calls))
    assert [c[0] for c in calls] == ["del_ready", "del_index", "swap",
                                     "ensure", "ready", "index"]
    assert set(calls[4][2]["callbacks"]) == {"authorize", "refresh"}
    assert registry.get_callback("plg-gmail--renew") is None
    assert registry.get_callback("plg-gmail--refresh") is not None


async def test_failed_rewrite_after_a_drop_and_add_leaves_neither_marker(
    tmp_path,
):
    """The point of retiring both: when the post-swap rewrite fails, the
    consumer reads 'facility unavailable' from BOTH files rather than a
    redirect URI for a callback the endpoint now 404s."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")
        _ack(acks, declared="renew")
        await _reconcile(
            registry, acks=acks, spool=spool,
            plugins=[_plugin(callbacks=("authorize", "renew"), path=str(art))])
        # consented only once the plugin declares it (an ack for an
        # undeclared name is exactly what the stale prune removes)
        _ack(acks, declared="refresh")
        ready = root / "gmail" / "ready.json"
        index = root / callback_spool.INDEX_DIR / \
            f"{callback_spool.index_key(str(art))}.json"
        assert ready.is_file() and index.is_file()

        class _WriteFails:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write_ready(self, plugin, payload):
                raise OSError("disk full")

        issues = await _reconcile(
            registry, acks=acks, spool=_WriteFails(spool),
            plugins=[_plugin(callbacks=("authorize", "refresh"),
                             path=str(art))])
        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
        assert registry.get_callback("plg-gmail--renew") is None
    finally:
        spool.close()


async def test_a_failed_rewrite_of_a_shrunk_set_leaves_no_marker(tmp_path):
    """The same fix's real point: when the post-swap rewrite fails, the
    operator is left with NO marker (fail-closed, the consumer sees the
    facility as unavailable) rather than one still advertising a callback the
    endpoint now 404s."""
    root = tmp_path / "spool"
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")
        _ack(acks, declared="renew")
        p2 = _plugin(callbacks=("authorize", "renew"))
        await _reconcile(registry, plugins=[p2], acks=acks, spool=spool)
        ready = root / "gmail" / "ready.json"
        import json
        assert set(json.loads(ready.read_text())["callbacks"]) == {
            "authorize", "renew"}

        class _WriteFails:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write_ready(self, plugin, payload):
                raise OSError("disk full")

        p1 = _plugin(callbacks=("authorize",))
        issues = await _reconcile(registry, plugins=[p1], acks=acks,
                                  spool=_WriteFails(spool))
        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not (root / callback_spool.INDEX_DIR /
                    f"{callback_spool.index_key(p1.path)}.json").exists()
        assert registry.get_callback("plg-gmail--renew") is None
    finally:
        spool.close()


async def test_growing_the_set_keeps_the_marker_through_the_swap(tmp_path):
    """The opposite direction is fail-closed (the marker under-advertises), so
    it must NOT churn the file — no delete, just the post-swap rewrite."""
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, declared="authorize")
    await _reconcile(registry, plugins=[_plugin(callbacks=("authorize",))],
                     acks=acks, spool=_SpoolStub(calls))
    calls.clear()
    _ack(acks, declared="renew")
    await _reconcile(registry,
                     plugins=[_plugin(callbacks=("authorize", "renew"))],
                     acks=acks, spool=_SpoolStub(calls))
    assert [c[0] for c in calls] == ["swap", "ensure", "ready", "index"]


async def test_index_key_is_the_resolved_artifact_path(tmp_path):
    """A real spool: the entry lands under sha256(realpath(artifact root)) —
    the one value a consumer provably knows."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    link = tmp_path / "linked"
    os.symlink(art, link)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(link))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        key = callback_spool.index_key(str(art))
        entry = root / callback_spool.INDEX_DIR / f"{key}.json"
        assert entry.is_file()
        import json
        assert json.loads(entry.read_text())["plugin_dir"] == "gmail"
        assert (root / "gmail" / "ready.json").is_file()
        assert (root / "gmail" / "pending").is_dir()
    finally:
        spool.close()


async def test_scoped_bundle_name_gets_its_own_spool_dir(tmp_path):
    """Bundled plugins register as ``slug.manifest-name`` — a dotted (but not
    dot-LEADING) name the spool accepts."""
    root = tmp_path / "spool"
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, plugin="finance.gmail")
        p = _plugin(name="finance.gmail")
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        assert registry.get_callback("plg-finance.gmail--authorize") is not None
        assert (root / "finance.gmail" / "ready.json").is_file()
    finally:
        spool.close()


async def test_spool_write_failure_surfaces_but_keeps_the_route(tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub(calls, fail={"ready"}))
    assert [i.reason_code for i in issues] == ["callback_spool_error"]
    # the overlay is the authority — an advisory-marker failure never unroutes
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_a_pathless_overlay_entry_never_reaches_the_index(tmp_path):
    """The index key is sha256(realpath(path)) and realpath("") is the process
    CWD — a malformed carried-over entry must never make the unroute delete a
    key derived from wherever casa happens to be running."""
    calls: list = []
    registry = _SpyRegistry(calls)
    registry.replace_callback_overlay({
        "plg-gmail--authorize": {"plugin": "gmail", "declared": "authorize"}})
    calls.clear()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    await _reconcile(registry, plugins=[], acks=acks, spool=_SpoolStub(calls),
                     entries=lambda: [])
    assert [c[0] for c in calls] == ["del_ready", "swap"]


async def test_missing_spool_reports_one_issue_per_plugin(tmp_path):
    """An unwired spool fails EVERY file operation — the health report gets
    one actionable row per plugin, not one per syscall (the unroute alone
    would otherwise contribute a marker row and an index row)."""
    registry = _SpyRegistry()
    registry.replace_callback_overlay({
        "plg-gone--old": {"plugin": "gone", "declared": "old",
                          "path": "/store/gone/art-0"}})
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks, spool=None,
                              entries=_entries(p))
    assert sorted((i.name, i.reason_code) for i in issues) == [
        ("gmail", "callback_spool_error"), ("gone", "callback_spool_error")]


async def test_missing_spool_still_swaps_the_overlay(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks, spool=None)
    assert [i.reason_code for i in issues] == ["callback_spool_error"]
    assert registry.get_callback("plg-gmail--authorize") is not None


# ---------------------------------------------------------------------------
# base URL
# ---------------------------------------------------------------------------


async def test_no_base_url_writes_no_files_and_reports_it(monkeypatch, tmp_path):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub(calls), base_url=None,
                              monkeypatch=monkeypatch)
    assert [i.reason_code for i in issues] == ["callback_base_url_invalid"]
    assert [c[0] for c in calls] == ["swap"]
    # consent still routes the overlay — the facility is merely unavailable
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_base_url_loss_retires_a_previously_published_marker(
    monkeypatch, tmp_path,
):
    calls: list = []
    registry = _SpyRegistry(calls)
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=_SpoolStub(calls))
    calls.clear()
    await _reconcile(registry, plugins=[p], acks=acks,
                     spool=_SpoolStub(calls), base_url=None,
                     monkeypatch=monkeypatch)
    assert [c[0] for c in calls] == ["del_ready", "del_index", "swap"]


# ---------------------------------------------------------------------------
# durable marker reconcile — survives a restart (on-disk inventory, not the
# in-memory previous overlay)
# ---------------------------------------------------------------------------


def _seed_prior_boot_marker(spool, plugin, art_path):
    """Write ready.json + the index entry directly, simulating a marker
    published by a PRIOR process (this process's in-memory overlay is empty,
    as it is right after a restart)."""
    spool.ensure_plugin_dirs(plugin)
    payload = {"v": 1, "base_url": BASE, "callbacks": {}}
    spool.write_ready(plugin, payload)
    spool.write_index_entry(str(art_path), dict(payload, plugin_dir=plugin))


def _marker_paths(root, plugin, art_path):
    return (root / plugin / "ready.json",
            root / callback_spool.INDEX_DIR /
            f"{callback_spool.index_key(str(art_path))}.json")


async def test_prior_boot_marker_retired_when_ack_now_absent(tmp_path):
    """A plugin routed in a PRIOR process whose ack is now gone: the durable
    reconcile retires ready.json AND the index entry (the in-memory previous
    overlay is empty, so only on-disk truth catches it), and the route is not
    served."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)
        assert ready.is_file() and index.is_file()

        registry = _SpyRegistry()               # empty overlay, like a boot
        acks = CallbackAckStore(path=tmp_path / "acks.json")   # NO ack
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)

        assert not ready.exists()
        assert not index.exists()
        assert registry.get_callback("plg-gmail--authorize") is None
        assert "gmail" not in spool.published_plugins()
    finally:
        spool.close()


async def test_prior_boot_marker_retired_when_base_url_now_invalid(
    monkeypatch, tmp_path,
):
    """Routed + acked, but the base URL is now invalid: nothing is publishable,
    so a marker from a prior boot (when it was valid) is retired."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         base_url=None, monkeypatch=monkeypatch)

        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_prior_boot_marker_retired_when_declaration_removed(tmp_path):
    """The plugin is no longer installed (declaration gone): its orphaned
    prior-boot marker is retired even though the in-memory overlay never
    carried it this process."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        await _reconcile(registry, plugins=[], acks=acks, spool=spool,
                         entries=lambda: [])

        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_durable_reconcile_preserves_a_still_routed_marker(tmp_path):
    """A plugin that IS in the desired routed set keeps its marker across a
    later reconcile with a fresh (empty) overlay."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, index = _marker_paths(root, "gmail", art)
        assert ready.is_file() and index.is_file()

        # A later "boot": fresh registry (empty overlay), same routed plugin.
        registry2 = _SpyRegistry()
        await _reconcile(registry2, plugins=[p], acks=acks, spool=spool)
        assert ready.is_file() and index.is_file()
        assert registry2.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_registry_invalid_does_not_retire_durable_markers(tmp_path):
    """Fail-closed availability: a wholesale compute failure (invalid registry
    ⇒ prunable False) must NOT nuke a valid plugin's on-disk marker."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_prior_boot_marker(spool, "gmail", art)
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         resolver=_resolver([p], valid=False))

        assert ready.is_file()
        assert index.is_file()
    finally:
        spool.close()


# ---------------------------------------------------------------------------
# durable marker reconcile — STILL-routed but payload changed while down. The
# in-memory swap diff is empty across a restart, so only the on-disk payload
# compare catches a dropped callback or a changed redirect base.
# ---------------------------------------------------------------------------


class _WriteFails:
    """Wrap a real spool, failing every ready.json write (a disk-full rewrite).
    Deletes and reads pass through, so a retire-before-swap still happens and a
    failed rewrite leaves the marker ABSENT rather than stale."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def write_ready(self, plugin, payload):
        raise OSError("disk full")


def _seed_marker(spool, plugin, art_path, callbacks, base=BASE):
    """Write a ready.json + index entry EXACTLY as a prior boot with this
    (plugin, callbacks, base) would have — via the same payload builder the
    reconcile writes with — so only the intended field (a dropped callback, a
    changed base) differs from a later desired pass."""
    routed = cr.RoutedCallbacks(
        plugin=plugin, artifact_id="art-seed", path=str(art_path),
        callbacks=[{"declared": d, "effective": f"plg-{plugin}--{d}"}
                   for d in callbacks])
    ready, index = cr._desired_marker_payloads(base, routed)
    spool.ensure_plugin_dirs(plugin)
    spool.write_ready(plugin, ready)
    spool.write_index_entry(str(art_path), index)


async def test_still_routed_dropped_callback_retired_across_restart(tmp_path):
    """A plugin STILL routed whose prior-boot marker advertises an EXTRA,
    now-dropped callback: the durable payload compare retires the stale marker
    before the swap and the rewrite advertises only the live callback."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"])
        ready, index = _marker_paths(root, "gmail", art)
        assert set(json.loads(ready.read_text())["callbacks"]) == {
            "authorize", "renew"}

        registry = _SpyRegistry()                 # empty overlay, like a boot
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")          # only authorize is live now
        p = _plugin(callbacks=("authorize",), path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)

        assert set(json.loads(ready.read_text())["callbacks"]) == {"authorize"}
        assert set(json.loads(index.read_text())["callbacks"]) == {"authorize"}
        assert registry.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_still_routed_stale_marker_absent_on_rewrite_failure(tmp_path):
    """The point of retiring before the swap: when the post-swap rewrite fails,
    the still-routed plugin's marker is left ABSENT (consumer reads 'not
    approved yet, wait'), never STALE advertising the dropped callback."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"])
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks, declared="authorize")
        p = _plugin(callbacks=("authorize",), path=str(art))
        issues = await _reconcile(registry, plugins=[p], acks=acks,
                                  spool=_WriteFails(spool))

        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_still_routed_base_url_change_retires_old_redirect(tmp_path):
    """A plugin still routed whose redirect BASE changed while down: the old
    redirect URI is gone after the reconcile (durable payload compare, not the
    empty in-memory diff)."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    old_base = "https://old.casa.example.org"
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize"], base=old_base)
        ready, index = _marker_paths(root, "gmail", art)
        assert old_base in ready.read_text()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)

        data = json.loads(ready.read_text())
        assert data["base_url"] == BASE
        assert old_base not in json.dumps(data)
        assert all(cb["redirect_uri"].startswith(BASE)
                   for cb in data["callbacks"].values())
        assert old_base not in index.read_text()
    finally:
        spool.close()


async def test_still_routed_base_url_change_absent_on_rewrite_failure(tmp_path):
    """The base-URL-change subcase is fail-closed on a rewrite failure too: the
    obsolete-redirect marker is retired before the swap, so a failed rewrite
    leaves it absent rather than advertising the old redirect URI forever."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize"],
                     base="https://old.casa.example.org")
        ready, index = _marker_paths(root, "gmail", art)

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        issues = await _reconcile(registry, plugins=[p], acks=acks,
                                  spool=_WriteFails(spool))

        assert [i.reason_code for i in issues] == ["callback_spool_error"]
        assert not ready.exists()
        assert not index.exists()
    finally:
        spool.close()


async def test_unchanged_still_routed_marker_is_not_rewritten(tmp_path):
    """No churn: when the on-disk payload already matches the desired one, a
    later reconcile (fresh empty overlay, as after a restart) neither deletes
    nor rewrites the marker — the file's inode and mtime are untouched."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool)
        ready, index = _marker_paths(root, "gmail", art)
        st_ready, st_index = ready.stat(), index.stat()

        class _Counting:
            def __init__(self, inner):
                self._inner = inner
                self.writes = 0
                self.deletes = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write_ready(self, *a):
                self.writes += 1
                return self._inner.write_ready(*a)

            def write_index_entry(self, *a):
                self.writes += 1
                return self._inner.write_index_entry(*a)

            def delete_ready(self, *a):
                self.deletes += 1
                return self._inner.delete_ready(*a)

            def delete_index_entry(self, *a):
                self.deletes += 1
                return self._inner.delete_index_entry(*a)

            def delete_index_key(self, *a):
                self.deletes += 1
                return self._inner.delete_index_key(*a)

        counting = _Counting(spool)
        registry2 = _SpyRegistry()                # fresh overlay = a restart
        await _reconcile(registry2, plugins=[p], acks=acks, spool=counting)

        assert counting.writes == 0               # no rewrite of a matching marker
        assert counting.deletes == 0              # and no retire
        assert ready.stat().st_ino == st_ready.st_ino
        assert ready.stat().st_mtime == st_ready.st_mtime
        assert index.stat().st_ino == st_index.st_ino
        assert registry2.get_callback("plg-gmail--authorize") is not None
    finally:
        spool.close()


async def test_registry_invalid_does_not_retire_a_differing_marker(tmp_path):
    """The double-gate holds for the payload compare too: an invalid-registry
    pass (``prunable`` False) must NOT retire even a marker whose payload
    differs from what a valid pass would desire — a transient bad compute may
    never delete a valid plugin's marker."""
    root = tmp_path / "spool"
    art = tmp_path / "store" / "gmail" / "art-1"
    art.mkdir(parents=True)
    spool = callback_spool.CallbackSpool(root)
    try:
        _seed_marker(spool, "gmail", art, ["authorize", "renew"],
                     base="https://old.casa.example.org")
        ready, index = _marker_paths(root, "gmail", art)
        before_ready = ready.read_text()

        registry = _SpyRegistry()
        acks = CallbackAckStore(path=tmp_path / "acks.json")
        _ack(acks)
        p = _plugin(path=str(art))
        await _reconcile(registry, plugins=[p], acks=acks, spool=spool,
                         resolver=_resolver([p], valid=False))

        assert ready.is_file() and index.is_file()
        assert ready.read_text() == before_ready   # untouched, not rewritten
    finally:
        spool.close()


@pytest.mark.parametrize("raw,expected", [
    ("https://casa.example.org", "https://casa.example.org"),
    ("https://casa.example.org/", "https://casa.example.org"),
    ("  https://casa.example.org  ", "https://casa.example.org"),
    ("null", None),
    ("None", None),
    ("", None),
])
def test_base_url_seam_reads_public_url(monkeypatch, raw, expected):
    monkeypatch.setenv("PUBLIC_URL", raw)
    assert _REAL_BASE_URL() == expected


def test_base_url_seam_without_public_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert _REAL_BASE_URL() is None


# ---------------------------------------------------------------------------
# the declaration digest — consent survives a routine upgrade
# ---------------------------------------------------------------------------


async def test_same_declaration_across_artifacts_keeps_the_ack(
    monkeypatch, tmp_path,
):
    """The consent identity excludes the artifact: an upgrade that leaves the
    declaration untouched stays routed with NO new prompt and no dark pass."""
    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    telegram = _FakeTelegram()
    p1 = _plugin()
    await _reconcile(registry, plugins=[p1], acks=acks, spool=_SpoolStub([]),
                     prompt=True, channel_manager=_FakeChannelManager(telegram))
    p2 = _plugin(artifact_id="art-2")
    issues = await _reconcile(registry, plugins=[p2], acks=acks,
                              spool=_SpoolStub([]), prompt=True,
                              channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None
    assert telegram.posts == []          # never re-prompted
    assert acks.get(_identity()) is not None


async def test_renamed_declaration_needs_fresh_consent(monkeypatch, tmp_path):
    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    telegram = _FakeTelegram()
    p2 = _plugin(artifact_id="art-2", callbacks=("authorise",))
    issues = await _reconcile(registry, plugins=[p2], acks=acks,
                              spool=_SpoolStub([]), prompt=True,
                              channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]
    assert registry.get_callback("plg-gmail--authorise") is None
    assert len(telegram.posts) == 1


# ---------------------------------------------------------------------------
# stale-ack prune
# ---------------------------------------------------------------------------


async def test_stale_ack_is_pruned_at_reconcile(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)                                 # gmail/authorize — installed
    _ack(acks, plugin="ghost", declared="old")  # nothing declares this
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]))
    assert acks.get(_identity()) is not None
    assert acks.get(_identity("ghost", "old")) is None


async def test_prune_keeps_acks_of_unassigned_and_unacked_declarations(tmp_path):
    """Prunability is about the DECLARATION existing, not about routing: an
    unassigned plugin's consent must survive so re-assignment needs no re-tap."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     entries=_entries(p, targets=[]))
    assert acks.get(_identity()) is not None


async def test_prune_is_skipped_when_the_registry_is_invalid(tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    await _reconcile(registry, plugins=[], acks=acks, spool=_SpoolStub([]),
                     resolver=_resolver([], valid=False), entries=lambda: [])
    assert acks.get(_identity()) is not None


async def test_prune_is_skipped_when_resolution_reported_issues(tmp_path):
    """An artifact hiccup (checksum, unreadable manifest) must never vaporize
    consent — the prune is opportunistic and waits for a clean pass."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    from plugin_registry import PluginIssue
    hiccup = PluginIssue(name="gmail", target=None, stage="resolve",
                         reason_code="artifact_invalid", artifact_id="art-1")
    await _reconcile(registry, plugins=[], acks=acks, spool=_SpoolStub([]),
                     resolver=_resolver([], issues=[hiccup]),
                     entries=lambda: [])
    assert acks.get(_identity()) is not None


async def test_prune_is_skipped_when_a_declaration_is_unparseable(tmp_path):
    """An invalid declaration contributes NO identities, so pruning
    that pass would destroy the operator's consent for the plugin's OTHER,
    perfectly valid callback — all-or-nothing rejects a set, it must never
    delete the acks behind it."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)                                   # gmail/authorize, consented
    p = _plugin(callbacks=("authorize", "plg-sneaky"))
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert [i.reason_code for i in issues] == ["callback_invalid"]
    assert acks.get(_identity()) is not None
    # and the consent is still there once the author fixes the declaration
    fixed = _plugin(callbacks=("authorize",))
    issues = await _reconcile(registry, plugins=[fixed], acks=acks,
                              spool=_SpoolStub([]), entries=_entries(fixed))
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None


async def test_one_invalid_plugin_suppresses_the_whole_prune(tmp_path):
    """The prune is global and opportunistic: another plugin's unreadable
    declaration is enough reason to wait for a pass that can read everything."""
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks, plugin="ghost", declared="old")   # genuinely stale
    good = _plugin()
    _ack(acks)
    bad = _plugin(name="badone", artifact_id="art-9", callbacks=("plg-nope",))
    await _reconcile(registry, plugins=[good, bad], acks=acks,
                     spool=_SpoolStub([]), entries=_entries(good, bad))
    assert acks.get(_identity("ghost", "old")) is not None
    # the next clean pass prunes it
    await _reconcile(registry, plugins=[good], acks=acks,
                     spool=_SpoolStub([]), entries=_entries(good))
    assert acks.get(_identity("ghost", "old")) is None


async def test_prune_failure_never_breaks_the_reconcile(monkeypatch, tmp_path):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)

    def _boom(valid_identities):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(acks, "prune_stale", _boom)
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]))
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None


# ---------------------------------------------------------------------------
# consent prompting
# ---------------------------------------------------------------------------


def _fresh_challenges(monkeypatch):
    import authz_grants
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = authz_grants.ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", coord)
    return broker, coord


async def test_pending_consent_fires_one_prompt(monkeypatch, tmp_path):
    _fresh_challenges(monkeypatch)
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     prompt=True, channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert "/callback/plg-gmail--authorize" in telegram.posts[0][2]
    # a second reconcile dedupes onto the live challenge
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     prompt=True, channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1


async def test_prompt_false_posts_nothing(monkeypatch, tmp_path):
    _fresh_challenges(monkeypatch)
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin()
    await _reconcile(registry, plugins=[p], acks=acks, spool=_SpoolStub([]),
                     prompt=False,
                     channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert telegram.posts == []


async def test_no_operator_channel_leaves_pending(monkeypatch, tmp_path):
    _fresh_challenges(monkeypatch)
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    issues = await _reconcile(registry, plugins=[p], acks=acks,
                              spool=_SpoolStub([]), prompt=True,
                              channel_manager=_FakeChannelManager(None))
    assert [i.reason_code for i in issues] == ["callback_pending_ack"]


# ---------------------------------------------------------------------------
# health recomputability
# ---------------------------------------------------------------------------


async def test_current_issues_recomputes_from_active_runtime(
    monkeypatch, tmp_path,
):
    import agent as agent_mod
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    runtime = SimpleNamespace(
        trigger_registry=_SpyRegistry(),
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)
    monkeypatch.setattr(cr, "_default_resolver", lambda: _resolver([p]))
    monkeypatch.setattr(cr, "_default_entries", lambda: _entries(p))
    monkeypatch.setattr(cr, "_default_acks", lambda: acks)
    assert [i.reason_code for i in cr.current_issues()] == [
        "callback_pending_ack"]
    _ack(acks)
    assert cr.current_issues() == []


async def test_current_issues_without_runtime_is_empty(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None)
    assert cr.current_issues() == []


async def test_current_issues_never_raises(monkeypatch):
    import agent as agent_mod
    runtime = SimpleNamespace(role_configs=_role_configs(assistant=["x"]))
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)

    def _boom():
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(cr, "_default_resolver", _boom)
    assert cr.current_issues() == []


# ---------------------------------------------------------------------------
# the runtime seam
# ---------------------------------------------------------------------------


async def test_reconcile_from_runtime_without_registry_is_a_noop():
    assert await cr.reconcile_from_runtime(None) == []
    assert await cr.reconcile_from_runtime(
        SimpleNamespace(trigger_registry=None)) == []


async def test_reconcile_from_runtime_uses_the_runtime_registry(
    monkeypatch, tmp_path,
):
    registry = _SpyRegistry()
    acks = CallbackAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    monkeypatch.setattr(cr, "_default_resolver", lambda: _resolver([p]))
    monkeypatch.setattr(cr, "_default_entries", lambda: _entries(p))
    monkeypatch.setattr(cr, "_default_acks", lambda: acks)
    monkeypatch.setattr(cr, "_default_spool", lambda: _SpoolStub([]))
    runtime = SimpleNamespace(
        trigger_registry=registry,
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    issues = await cr.reconcile_from_runtime(runtime, prompt=False)
    assert issues == []
    assert registry.get_callback("plg-gmail--authorize") is not None
