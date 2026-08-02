"""Acceptance rehearsal for the authorization-callback facility.

ONE end-to-end, unit-level (no container) walkthrough that drives the WHOLE
facility as an integrated whole:

* ``plugin_callbacks`` (declaration parse + digest/identity),
* ``callback_acks`` (persistent operator consent),
* ``callback_reconcile`` (the overlay + ``ready.json`` / ``.index`` writer,
  base URL via the ``callback_urls.validated_base`` seam),
* ``callback_spool`` (mint / claim / publish-once / index discovery),
* ``callback_http`` (the unauthenticated ``GET /callback/{name}`` endpoint),
* ``callback_episodes`` (the at-least-once delivery nudge).

The four scenarios below walk through:

(a) **gmail shape** — register → consent → reconcile routes + publishes
    ready/index → a consumer discovers its spool by
    ``sha256(realpath(plugin_root))``, reads its ``redirect_uri``, mints a
    state, the provider redirect lands via a real aiohttp client (303 →
    ``/callback/done``), the result is published, the delivery nudge is kicked
    with the handle, and the result is collected via the documented
    ``results/.collect-<hash>-<uuid>`` claim-rename discipline.
(b) **finance renewal loop** — a second mint/redirect/collect for the SAME
    routed callback after the first has settled (the 180-day renewal shape).
(c) **consumer-dead rehearsal** — a result published with no poller;
    ``callback_episodes.recovery`` re-enqueues its nudge.
(d) **declaration-digest stability** — an artifact-id change with an unchanged
    declaration keeps the ack and the routing with no re-prompt.

This is a conformance test: any failure here is a bug in Tasks 1-9, not
something to paper over in the test.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import callback_episodes as ce
import callback_http
import callback_reconcile as cr
import callback_spool
import callback_urls
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity, declaration_digest, effective_name
from trigger_registry import TriggerRegistry
from yarl import URL

BASE = "https://casa.example.org"


# ---------------------------------------------------------------------------
# a plugin the resolver/registry can serve
# ---------------------------------------------------------------------------


def _manifest(names):
    return {"name": "x", "casa": {"callbacks": [{"name": n} for n in names]}}


def _plugin(*, name, artifact_id, path, callbacks):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id, path=path,
        version="1.0.0", manifest_name=name, manifest=_manifest(callbacks))


def _resolver(plugins):
    def resolve(target):
        return SimpleNamespace(registry_valid=True, plugins=list(plugins),
                               issues=[])
    return resolve


def _entries(*plugins, targets=("resident:assistant",)):
    rows = [{"name": p.name, "artifact_id": p.artifact_id,
             "targets": list(targets)} for p in plugins]

    def provider():
        return rows
    return provider


def _role_configs():
    return {"assistant": SimpleNamespace(channels=["telegram"])}


# ---------------------------------------------------------------------------
# the facility harness — a REAL spool + registry + acks + wired episodes
# ---------------------------------------------------------------------------


class _Facility:
    """Everything a consumer and the endpoint need, on a real filesystem."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.spool_root = tmp_path / "callbacks"
        self.store_root = tmp_path / "store"
        self.spool = callback_spool.CallbackSpool(self.spool_root)
        self.registry = TriggerRegistry(scheduler=None, app=None, bus=None)
        self.acks = CallbackAckStore(path=tmp_path / "callback_acks.json")

        # The public base URL flows through the validated-base seam so the redirect
        # URIs a consumer reads are the validated origin, not a raw string.
        monkeypatch.setattr(
            cr, "_base_url",
            lambda: callback_urls.validated_base({"PUBLIC_URL": BASE}))

        # Wire the delivery-nudge module against this spool, on a private
        # ledger, with a recording dispatch double (mirrors
        # tests/test_callback_episodes.py's fixture — never patch a global
        # asyncio.sleep).
        monkeypatch.setattr(ce, "STORE_PATH",
                            tmp_path / "callback-episodes.json")
        monkeypatch.setattr(ce, "_worker_task", None)
        monkeypatch.setattr(ce, "_lock", None)
        monkeypatch.setattr(ce, "_kick", None)
        ce._pending_hints.clear()
        self.dispatches: list[tuple[str, str, dict]] = []
        self._targets = ["resident:assistant"]

        async def _dispatch(role, text, context):
            self.dispatches.append((role, text, context))
            return True

        async def _sleep(_s):
            return None

        ce.configure(
            dispatch=_dispatch,
            resolve_registry_entry=lambda plugin: {"targets": self._targets},
            get_spool=lambda: self.spool,
            sleep=_sleep,
        )

    def close(self) -> None:
        self.spool.close()

    # -- artifact + consent + reconcile -------------------------------------

    def make_artifact(self, name: str, artifact_id: str) -> Path:
        art = self.store_root / name / artifact_id
        art.mkdir(parents=True, exist_ok=True)
        return art

    def consent(self, plugin: str, declared: str) -> str:
        """Record the operator's ack for one declared callback; return the
        consent identity."""
        eff = effective_name(plugin, declared)
        digest = declaration_digest({"declared": declared, "effective": eff})
        self.acks.record(plugin=plugin, effective=eff,
                         declaration_digest=digest)
        return ack_identity(plugin, eff, digest)

    async def reconcile(self, plugin) -> list:
        return await cr.reconcile_plugin_callbacks(
            trigger_registry=self.registry, role_configs=_role_configs(),
            acks=self.acks, spool=self.spool, resolver=_resolver([plugin]),
            entries=_entries(plugin), prompt=False)

    # -- the consumer's half ---------------------------------------------

    def discover(self, plugin_root: Path) -> dict:
        """A consumer knows only ``realpath($CLAUDE_PLUGIN_ROOT)``; it reads
        ``.index/<sha256(realpath)>.json`` to find its spool dir + redirect
        URIs, exactly as a real plugin would."""
        key = callback_spool.index_key(str(plugin_root))
        entry = self.spool_root / callback_spool.INDEX_DIR / f"{key}.json"
        return json.loads(entry.read_text())

    def plugin_dir(self, plugin_dir_name: str) -> Path:
        return self.spool_root / plugin_dir_name

    def results_dir(self, plugin_dir_name: str) -> Path:
        return self.plugin_dir(plugin_dir_name) / callback_spool.RESULTS_DIR

    def ready_payload(self, plugin_dir_name: str) -> dict:
        return json.loads(
            (self.plugin_dir(plugin_dir_name)
             / callback_spool.READY_NAME).read_text())

    def collect(self, plugin_dir_name: str, state_hash_hex: str) -> dict:
        """The documented ``results/.collect-<hash>-<uuid>`` discipline: claim
        the published result by an atomic rename to a consumer-held name (only
        one collector can win the rename), read it, then unlink. The claimed
        name is excluded from the per-plugin cap and ages out on its own TTL if
        the reader dies mid-collect."""
        results = self.results_dir(plugin_dir_name)
        src = results / f"{state_hash_hex}.json"
        held = results / f"{callback_spool.COLLECT_PREFIX}{state_hash_hex}-{uuid.uuid4().hex}"
        os.rename(src, held)
        record = json.loads(held.read_text())
        os.unlink(held)
        return record


@pytest.fixture()
def facility(tmp_path, monkeypatch):
    fac = _Facility(tmp_path, monkeypatch)
    try:
        yield fac
    finally:
        fac.close()


# ---------------------------------------------------------------------------
# aiohttp endpoint harness (reuses tests/test_callback_http.py idioms)
# ---------------------------------------------------------------------------


def _build_app(facility: _Facility) -> web.Application:
    app = web.Application()
    handler = callback_http.make_callback_handler(
        trigger_registry=facility.registry,
        spool_provider=lambda: facility.spool,
    )
    # Registration order is load-bearing: the static done route must win over
    # the wildcard.
    app.router.add_get("/callback/done", callback_http.make_done_handler())
    app.router.add_get("/callback/{name}", handler)
    return app


async def _redirect(client, effective: str, state: str, *, code="AUTHCODE"):
    """Drive the provider's browser redirect through the real endpoint,
    byte-exact (``encoded=True`` keeps the state off the client's re-encode
    path) and without following the 303."""
    target = f"/callback/{effective}?code={code}&state={state}"
    return await client.get(URL(target, encoded=True), allow_redirects=False)


def _fresh_state() -> str:
    """A consumer-minted state in the endpoint's grammar ([A-Za-z0-9._~-],
    22-256 chars)."""
    import secrets
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# (a) gmail shape — the full happy path, register → redirect → collect → nudge
# ---------------------------------------------------------------------------


async def test_gmail_shape_end_to_end(facility):
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    art = facility.make_artifact(plugin, "art-1")
    p = _plugin(name=plugin, artifact_id="art-1", path=str(art),
                callbacks=(declared,))

    # register + operator consent + reconcile
    identity = facility.consent(plugin, declared)
    issues = await facility.reconcile(p)
    assert issues == []
    assert facility.registry.get_callback(eff) is not None
    assert facility.acks.get(identity) is not None

    # the reconcile published the readiness marker + the discovery index entry
    assert (facility.plugin_dir(plugin) / callback_spool.READY_NAME).is_file()

    # consumer side: discover the spool by sha256(realpath(plugin_root)),
    # read the redirect_uri from the ready/index payload
    index = facility.discover(art)
    assert index["plugin_dir"] == plugin
    assert index["base_url"] == BASE
    redirect_uri = index["callbacks"][declared]["redirect_uri"]
    assert redirect_uri == f"{BASE}/callback/{eff}"
    # the ready.json marker carries the same map
    assert facility.ready_payload(plugin)["callbacks"][declared][
        "redirect_uri"] == redirect_uri

    # consumer mints a state into its own spool dir, registers redirect_uri
    # with its provider, and hands control to the browser
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    callback_spool.mint(facility.plugin_dir(plugin), state)

    # the provider's browser redirect lands at the endpoint
    app = _build_app(facility)
    async with TestClient(TestServer(app)) as client:
        r = await _redirect(client, eff, state)
    assert r.status == 303
    assert r.headers["Location"] == "/callback/done"

    # the result was published, publish-once, into results/<hash>.json
    assert (facility.results_dir(plugin) / f"{h}.json").is_file()

    # the delivery nudge was kicked with the handle: the HTTP handler recorded
    # an in-memory hint; the worker turns it into a dispatched turn naming the
    # exact handle (result still present)
    assert (plugin, h) in ce._pending_hints
    await ce._worker_pass()
    assert len(facility.dispatches) == 1
    role, instruction, context = facility.dispatches[0]
    assert role == "assistant"
    assert f"(handle {h})" in instruction
    assert context["synthetic"] == "callback_nudge"

    # the agent collects the code via the claim-rename discipline
    record = facility.collect(plugin, h)
    assert record["v"] == 1
    assert record["plugin"] == plugin
    assert record["effective"] == eff
    assert record["raw_query"] == f"code=AUTHCODE&state={state}"
    assert ["code", "AUTHCODE"] in record["query"]
    assert ["state", state] in record["query"]

    # collected: the result file is gone, and a recovery/worker pass settles
    # the episode + its tombstone so nothing lingers
    assert not (facility.results_dir(plugin) / f"{h}.json").exists()
    await ce.recovery(facility.spool)
    assert ce.episodes() == []
    assert ce._load()["tombstones"] == []


# ---------------------------------------------------------------------------
# (b) finance renewal loop — a second full flow on the SAME callback
# ---------------------------------------------------------------------------


async def test_finance_renewal_loop(facility):
    plugin, declared = "finance", "renew"
    eff = effective_name(plugin, declared)
    art = facility.make_artifact(plugin, "art-1")
    p = _plugin(name=plugin, artifact_id="art-1", path=str(art),
                callbacks=(declared,))

    facility.consent(plugin, declared)
    assert await facility.reconcile(p) == []
    assert facility.registry.get_callback(eff) is not None

    index = facility.discover(art)
    assert index["callbacks"][declared]["redirect_uri"] == \
        f"{BASE}/callback/{eff}"

    app = _build_app(facility)

    async def _one_flow() -> str:
        state = _fresh_state()
        h = callback_spool.state_hash(state)
        callback_spool.mint(facility.plugin_dir(plugin), state)
        async with TestClient(TestServer(app)) as client:
            r = await _redirect(client, eff, state, code=f"code-{h[:6]}")
        assert r.status == 303
        await ce._worker_pass()
        record = facility.collect(plugin, h)
        assert record["plugin"] == plugin
        return h

    # first authorization settles fully (collected + episode pruned)
    h1 = await _one_flow()
    await ce.recovery(facility.spool)
    assert ce.episodes() == []

    # 180 days later the same callback is re-exercised — a fresh state, a fresh
    # hash, the SAME routed effective name. It must work identically.
    h2 = await _one_flow()
    assert h2 != h1
    assert len(facility.dispatches) == 2
    assert f"(handle {h2})" in facility.dispatches[-1][1]


# ---------------------------------------------------------------------------
# (c) consumer-dead rehearsal — recovery re-enqueues an orphaned result
# ---------------------------------------------------------------------------


async def test_consumer_dead_recovery_reenqueues_the_nudge(facility):
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    art = facility.make_artifact(plugin, "art-1")
    p = _plugin(name=plugin, artifact_id="art-1", path=str(art),
                callbacks=(declared,))
    facility.consent(plugin, declared)
    assert await facility.reconcile(p) == []

    # A result lands with NO poller: publish it directly through the spool
    # (a redirect whose in-memory kick was lost to a crash), and run NO worker
    # pass — so no episode exists for it yet.
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    callback_spool.mint(facility.plugin_dir(plugin), state)
    claim = facility.spool.claim(plugin, h, now=time.time())
    assert claim is not None
    assert facility.spool.publish_result(claim, {"v": 1, "plugin": plugin,
                                                 "effective": eff}) is True
    assert ce.episodes() == []           # nothing enqueued it

    # the recovery invariant is the backstop: it enqueues a pending episode for
    # any result lacking an episode/tombstone
    await ce.recovery(facility.spool)
    pending = ce.episodes("pending")
    assert [(e["plugin"], e["result_hash"]) for e in pending] == [(plugin, h)]

    # and the worker then dispatches the (re-enqueued) nudge with the handle
    await ce._worker_pass()
    assert len(facility.dispatches) == 1
    assert f"(handle {h})" in facility.dispatches[0][1]


# ---------------------------------------------------------------------------
# (d) declaration-digest stability — an artifact bump keeps ack + routing
# ---------------------------------------------------------------------------


async def test_declaration_digest_survives_artifact_change(facility):
    plugin, declared = "gmail", "authorize"
    eff = effective_name(plugin, declared)
    identity = facility.consent(plugin, declared)

    art1 = facility.make_artifact(plugin, "art-1")
    p1 = _plugin(name=plugin, artifact_id="art-1", path=str(art1),
                 callbacks=(declared,))
    assert await facility.reconcile(p1) == []
    assert facility.registry.get_callback(eff) is not None
    # discoverable under the art-1 path
    assert facility.discover(art1)["plugin_dir"] == plugin

    # a routine upgrade: SAME declaration, new artifact id + path. The consent
    # identity binds the declaration digest, not the artifact, so the ack (and
    # the routing) survive with no re-prompt and no dark window.
    art2 = facility.make_artifact(plugin, "art-2")
    p2 = _plugin(name=plugin, artifact_id="art-2", path=str(art2),
                 callbacks=(declared,))
    issues = await facility.reconcile(p2)
    assert issues == []                                  # no callback_pending_ack
    assert facility.registry.get_callback(eff) is not None
    assert facility.acks.get(identity) is not None       # ack untouched

    # the discovery index followed the artifact: new path present, old retired
    index2 = facility.discover(art2)
    assert index2["callbacks"][declared]["redirect_uri"] == \
        f"{BASE}/callback/{eff}"
    old_key = callback_spool.index_key(str(art1))
    assert not (facility.spool_root / callback_spool.INDEX_DIR
                / f"{old_key}.json").exists()

    # a full flow still works on the upgraded artifact
    state = _fresh_state()
    h = callback_spool.state_hash(state)
    callback_spool.mint(facility.plugin_dir(plugin), state)
    app = _build_app(facility)
    async with TestClient(TestServer(app)) as client:
        r = await _redirect(client, eff, state)
    assert r.status == 303
    record = facility.collect(plugin, h)
    assert record["plugin"] == plugin
    assert record["effective"] == eff
