"""v0.146 → v0.147 boot migration — the episode store retires AFTER the
attempt ledger has materialized (spec §12).

The ordering is the whole point. Once ``/data/callback-episodes.json`` is
gone, the only thing naming a live pre-upgrade flow is the artifact union in
the spool — and for a flow the consumer has already renamed into a
``.collect-<h>-<uuid>`` hold, that name is *all* casa has. So the boot seam
must run ``attempts_pass(boot=True)`` first and unlink the store second; the
reverse order silently drops those flows.

Driven through the REAL seam (``casa_core._boot_reconcile_plugin_callbacks``)
against a REAL spool on a real filesystem, with only the reconcile and the
registry snapshot faked out — a fake spool would pin nothing here, since the
migration's content IS what the pass derives from the artifacts.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import callback_spool

PLUGIN = "gmail"
H_PENDING = "1" * 64        # a v1-envelope mint still awaiting the redirect
H_RESULT = "2" * 64         # a published, uncollected result
H_HELD = "3" * 64           # a result the consumer already renamed into a hold


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _seed_v0146_spool(root: Path) -> callback_spool.CallbackSpool:
    """A spool in exactly the shape a v0.146 upgrade finds it: artifacts in
    every phase and NOT ONE attempt file (the ledger did not exist yet)."""
    spool = callback_spool.CallbackSpool(root)
    spool.ensure_plugin_dirs(PLUGIN)
    pdir = root / PLUGIN

    # (i) a pending minted by v0.146 — a v1 envelope, no meta.
    (pdir / callback_spool.PENDING_DIR / f"{H_PENDING}.json").write_bytes(
        callback_spool.canonical_marker_bytes({"v": 1}))

    # (ii) a published result — the v0.146 record shape (no meta/minted_ts).
    (pdir / callback_spool.RESULTS_DIR / f"{H_RESULT}.json").write_text(
        json.dumps({"v": 1, "plugin": PLUGIN, "query": {"code": "x"}}),
        encoding="utf-8")

    # (iii) a consumer-held hold: the flow casa can only see by NAME.
    held = (f"{callback_spool.COLLECT_PREFIX}{H_HELD}-{uuid.uuid4().hex}")
    (pdir / callback_spool.RESULTS_DIR / held).write_text(
        json.dumps({"v": 1, "plugin": PLUGIN}), encoding="utf-8")

    assert not (pdir / callback_spool.ATTEMPTS_DIR).exists() or not list(
        (pdir / callback_spool.ATTEMPTS_DIR).iterdir())
    return spool


def _wire_boot(monkeypatch, spool, tmp_path) -> Path:
    """Fake out everything the boot seam touches EXCEPT the spool and the
    migration, and point the legacy-store constant at a tmp file that exists.
    Returns that path."""
    import callback_episodes
    import callback_reconcile
    import plugin_registry

    monkeypatch.setattr(callback_spool, "get_spool", lambda: spool)

    async def _noop_recon(**_kw):
        return []

    monkeypatch.setattr(callback_reconcile, "reconcile_plugin_callbacks",
                        _noop_recon)
    # An INVALID registry keeps the gated orphan GC a no-op, so the migration
    # is the only thing acting on the spool dir.
    monkeypatch.setattr(plugin_registry, "snapshot_registry",
                        lambda: SimpleNamespace(valid=False, entries=[]))

    legacy = tmp_path / "callback-episodes.json"
    legacy.write_text(json.dumps({"episodes": [{"plugin": PLUGIN}]}),
                      encoding="utf-8")
    monkeypatch.setattr(callback_episodes, "LEGACY_STORE_PATH", legacy)
    return legacy


def _attempts(spool) -> dict:
    return dict(spool.list_attempts(PLUGIN))


# ---------------------------------------------------------------------------
# the migration itself
# ---------------------------------------------------------------------------


async def test_boot_materializes_the_union_then_retires_the_store(
        tmp_path, monkeypatch):
    """The v0.146→v0.147 upgrade boot: after one pass the legacy store is
    gone and every artifact casa can see has an attempt record — including
    the ``.collect-*``-held flow, whose record must carry the "known by name
    only" shape (``meta``/``minted_ts`` None, ``claimed`` True)."""
    import casa_core

    spool = _seed_v0146_spool(tmp_path / "callbacks")
    legacy = _wire_boot(monkeypatch, spool, tmp_path)
    try:
        await casa_core._boot_reconcile_plugin_callbacks(
            trigger_registry=object(), role_configs={})

        assert not legacy.exists(), "the legacy episode store must be retired"

        recs = _attempts(spool)
        assert set(recs) == {H_PENDING, H_RESULT, H_HELD}, \
            "every artifact in the union must have materialized a record"

        # (iii) the Sol r1 S10 / B8 pin — a pre-upgrade collect-held flow is
        # NOT lost, and casa never opened the file to learn about it.
        held = recs[H_HELD]
        assert held["status"] == "result_ready"
        assert held["claimed"] is True
        assert held["meta"] is None
        assert held["minted_ts"] is None

        # (i)/(ii) the other two phases keep their own shapes.
        assert recs[H_PENDING]["status"] == "awaiting_redirect"
        assert recs[H_PENDING]["meta"] is None      # a v1 envelope carries none
        assert recs[H_RESULT]["status"] == "result_ready"
        assert recs[H_RESULT]["claimed"] is False
    finally:
        spool.close()


async def test_boot_is_idempotent_and_absent_store_is_not_an_error(
        tmp_path, monkeypatch):
    """A second boot has no store to delete and must neither fail nor
    disturb the ledger it already materialized."""
    import casa_core

    spool = _seed_v0146_spool(tmp_path / "callbacks")
    legacy = _wire_boot(monkeypatch, spool, tmp_path)
    try:
        await casa_core._boot_reconcile_plugin_callbacks(
            trigger_registry=object(), role_configs={})
        first = _attempts(spool)
        assert not legacy.exists()

        await casa_core._boot_reconcile_plugin_callbacks(
            trigger_registry=object(), role_configs={})
        assert set(_attempts(spool)) == set(first)
    finally:
        spool.close()


async def test_a_failed_unlink_never_breaks_boot(tmp_path, monkeypatch):
    """The retirement is best-effort: an unlink that raises leaves the store
    for the next boot and must not abort the reconcile or the GC behind it."""
    import casa_core

    spool = _seed_v0146_spool(tmp_path / "callbacks")
    legacy = _wire_boot(monkeypatch, spool, tmp_path)

    class _Stubborn(type(legacy)):        # same flavour of concrete Path
        def unlink(self, *a, **k):
            raise OSError("read-only /data")

    import callback_episodes
    monkeypatch.setattr(callback_episodes, "LEGACY_STORE_PATH",
                        _Stubborn(legacy))
    try:
        await casa_core._boot_reconcile_plugin_callbacks(
            trigger_registry=object(), role_configs={})
        assert legacy.exists(), "a failed unlink leaves the store in place"
        # the pass before it still ran — the ledger is materialized regardless
        assert set(_attempts(spool)) == {H_PENDING, H_RESULT, H_HELD}
    finally:
        spool.close()


async def test_materialization_precedes_the_store_deletion(
        tmp_path, monkeypatch):
    """Order pin (spec §12): the attempts pass must have completed BEFORE the
    store is unlinked. Recorded from inside ``attempts_pass`` — if the seam is
    ever reordered, the store is already gone when the pass runs."""
    import casa_core

    spool = _seed_v0146_spool(tmp_path / "callbacks")
    legacy = _wire_boot(monkeypatch, spool, tmp_path)
    real = spool.attempts_pass
    store_seen: list[bool] = []

    def _record(**kwargs):
        store_seen.append(legacy.exists())
        return real(**kwargs)

    spool.attempts_pass = _record          # type: ignore[method-assign]
    try:
        await casa_core._boot_reconcile_plugin_callbacks(
            trigger_registry=object(), role_configs={})
        assert store_seen == [True], \
            "attempts_pass must run while the legacy store still exists"
        assert not legacy.exists()
    finally:
        spool.close()


# ---------------------------------------------------------------------------
# in-flight discipline — boot reconciles everything, the periodic pass does not
# ---------------------------------------------------------------------------


async def test_boot_seam_reconciles_every_hash(tmp_path, monkeypatch):
    """``boot=True`` at the boot seam: no handler exists yet, so no hash is
    skipped."""
    import casa_core

    spool = _seed_v0146_spool(tmp_path / "callbacks")
    _wire_boot(monkeypatch, spool, tmp_path)
    seen: list[dict] = []
    real = spool.attempts_pass

    def _record(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    spool.attempts_pass = _record          # type: ignore[method-assign]
    try:
        await casa_core._boot_reconcile_plugin_callbacks(
            trigger_registry=object(), role_configs={})
        assert [k["boot"] for k in seen] == [True]
    finally:
        spool.close()


async def test_periodic_recovery_uses_boot_false_semantics(tmp_path):
    """The PERIODIC caller must pass ``boot=False`` — a pass running beside a
    live handler has to keep the in-flight skip. The default stays ``True``
    for the boot seam."""
    import callback_episodes

    spool = _seed_v0146_spool(tmp_path / "callbacks")
    seen: list[dict] = []

    def _record(**kwargs):
        seen.append(kwargs)
        return callback_spool.AttemptsReport()

    spool.attempts_pass = _record          # type: ignore[method-assign]
    try:
        await callback_episodes.recovery(spool, boot=False)
        await callback_episodes.recovery(spool)
        assert [k["boot"] for k in seen] == [False, True]
    finally:
        spool.close()


def test_scheduled_recovery_job_passes_boot_false():
    """The periodic job is a closure inside ``main()``; pin its call shape at
    the source, the way the lock-stall ruling is pinned."""
    src = (Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt"
           / "casa" / "casa_core.py").read_text(encoding="utf-8")
    flat = " ".join(src.split())
    assert "await _cbep.recovery(spool, boot=False)" in flat
    # ...and the boot seam runs the attempts pass off-loop with boot=True.
    assert "asyncio.to_thread(spool.attempts_pass, now=time.time(), boot=True)" \
        in flat
