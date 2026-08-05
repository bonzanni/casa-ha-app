"""Plugin-event WIRING (boot, scheduler, reload scope, plugin lifecycle,
health merge).

Structurally the sibling of ``tests/test_callback_wiring.py`` (read that
file whole before touching this one — its idioms, including source-level
structural pins for logic nested inside ``casa_core.main()``, are mirrored
here). These pin the load-bearing wiring requirements Task 10 adds:

* **Boot order.** The event spool is initialised, the routing map is
  reconciled, and ONLY THEN does the delivery worker's boot recovery run —
  and that recovery MUST complete before ``start_worker()`` (mirrors the
  callback boot pin at ``casa_core.py``'s ``_boot_reconcile_plugin_callbacks``
  docstring: a routed pair's ready state must be published before anything
  can dispatch against it).
* **Periodic liveness.** The scheduled sweep/recovery jobs exist, run off
  the loop (the lock-stall ruling), and the recovery job additionally
  retries the ROUTING_UNAVAILABLE-sentinel compute on its own — a transient
  reconcile failure must not silently suspend delivery until an unrelated
  plugin-lifecycle mutation happens to retry it.
* **Reconcile pairing.** Every site that reconciles triggers/callbacks
  (reload's trigger-affecting scopes, the 5-mutation lifecycle sequencer)
  also reconciles events.
* **Health merge.** ``event_reconcile.current_issues()`` (a list of DICTS,
  never ``PluginIssue`` instances) is folded into
  ``tools._regenerate_plugin_health``'s report, concatenated directly —
  never through the ``PluginIssue``-attribute-only ``_add``/
  ``_rediscoverable`` helpers.
* **Fail-closed registry.** ``get_installed``/``get_registry_valid`` never
  fabricate membership from an invalid registry snapshot.
* **event_wake is casa-internal only.** The synthetic marker an event nudge
  rides is a RESERVED context key — unreachable from any external ingress —
  and a turn dispatched under it fails ask_user's provenance gate exactly
  like a callback/setup nudge does.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CASA = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
_SRC = (CASA / "casa_core.py").read_text(encoding="utf-8")
_FLAT = " ".join(_SRC.split())


# ---------------------------------------------------------------------------
# Boot order — spool init -> boot reconcile -> configure -> recovery ->
# start_worker, with "recovery before start_worker" as the pinned invariant.
# ---------------------------------------------------------------------------


class TestBootOrder:
    def test_event_spool_initialised(self):
        assert "event_spool.init_spool()" in _SRC

    def test_boot_reconcile_events_function_exists(self):
        assert "async def _boot_reconcile_plugin_events(" in _SRC
        assert "await _boot_reconcile_plugin_events(" in _SRC

    def test_boot_order_spool_init_then_reconcile_then_configure(self):
        idx_spool_init = _SRC.index("event_spool.init_spool()")
        idx_boot_reconcile_call = _SRC.index(
            "await _boot_reconcile_plugin_events(")
        idx_configure = _SRC.index("_evep.configure(")
        assert idx_spool_init < idx_boot_reconcile_call < idx_configure

    def test_recovery_runs_before_start_worker(self):
        """The load-bearing pin: event_episodes.recovery(boot=True) MUST be
        awaited before start_worker() is called — otherwise the worker's
        first pass could race the boot reconstruction of the delivery
        ledger."""
        idx_recovery = _SRC.index("await _evep.recovery(boot=True)")
        idx_start_worker = _SRC.index("_evep.start_worker()")
        assert idx_recovery < idx_start_worker

    def test_boot_reconcile_events_paired_after_callbacks(self):
        """Mirrors test_i3_boot_pairs_trigger_and_callback_reconcile: the
        events boot reconcile call site sits AFTER the callbacks one (Task
        10 is explicitly "ordered AFTER the callback block")."""
        idx_callbacks = _SRC.index("await _boot_reconcile_plugin_callbacks(")
        idx_events = _SRC.index("await _boot_reconcile_plugin_events(")
        assert idx_callbacks < idx_events

    def test_boot_reconcile_events_never_fatal(self):
        """The boot seam's docstring/body must degrade to the fail-closed
        sentinel on a compute failure, never crash boot (mirrors triggers/
        callbacks' own never-fatal boot seams)."""
        import casa_core
        import inspect
        src = inspect.getsource(casa_core._boot_reconcile_plugin_events)
        assert "except Exception" in src

    def test_worker_closures_wired_from_the_setup_seams(self):
        """dispatch/notify_operator reuse the SAME late-binding seams the
        setup-episode + callback workers use; get_spool/get_routed are bare
        references to the live module functions (never a one-shot
        snapshot)."""
        start = _SRC.index("_evep.configure(")
        end = _SRC.index(")", start)
        block = _SRC[start:end]
        assert "dispatch=_setup_dispatch" in block
        assert "notify_operator=_setup_notify" in block
        assert "resolve_registry_entry=_event_registry_entry" in block
        assert "get_routed=_evrec.get_routed" in block
        assert "get_installed=_event_installed" in block
        assert "get_registry_valid=_event_registry_valid" in block
        assert "get_spool=event_spool.get_spool" in block


# ---------------------------------------------------------------------------
# Periodic jobs — registered, off-loop, sentinel-retry liveness.
# ---------------------------------------------------------------------------


class TestPeriodicJobs:
    def test_jobs_registered(self):
        assert 'id="event_spool_sweep"' in _SRC
        assert 'id="event_spool_recovery"' in _SRC

    def test_sweep_runs_off_the_loop(self):
        """Lock-stall ruling (mirrors test_lock_stall_scheduled_scans_use_
        to_thread): the scheduled scan holds the spool's lock for a whole
        pass, so it must run via asyncio.to_thread, never inline."""
        assert "asyncio.to_thread( spool.sweep, routed, installed, valid, time.time())" \
            in _FLAT

    def test_recovery_job_uses_periodic_boot_false(self):
        idx = _SRC.index("async def _event_spool_recovery")
        end = _SRC.index("scheduler.add_job(", idx)
        block = _SRC[idx:end]
        assert "_evep.recovery(boot=False)" in block

    def test_periodic_recovery_retries_reconcile_under_sentinel(self):
        """Liveness (Task 10): the periodic job must notice a stuck
        ROUTING_UNAVAILABLE sentinel and retry the compute itself (via the
        SAME kick() the pre-send gate uses on a consent mismatch) — a
        transient compute failure otherwise waits forever for an unrelated
        lifecycle mutation to retry it."""
        import ast

        tree = ast.parse(_SRC)

        # Find the _event_spool_recovery function (nested in main)
        func_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and \
               node.name == "_event_spool_recovery":
                func_def = node
                break

        assert func_def is not None, \
            "Function _event_spool_recovery not found"

        # Find the If node whose test contains ROUTING_UNAVAILABLE comparison
        sentinel_if = None
        for stmt in func_def.body:
            if isinstance(stmt, ast.If):
                # Check if the test contains the ROUTING_UNAVAILABLE reference
                test_src = ast.unparse(stmt.test) \
                    if hasattr(ast, 'unparse') else ""
                if "ROUTING_UNAVAILABLE" in test_src:
                    sentinel_if = stmt
                    break

        assert sentinel_if is not None, \
            "If statement with ROUTING_UNAVAILABLE comparison not found"

        # Verify that kick() is called inside the If body
        kick_in_if = False
        for node in ast.walk(sentinel_if):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and \
                   node.func.attr == "kick":
                    kick_in_if = True
                    break

        assert kick_in_if, "kick() call not found inside the If body"

        # Verify that NO kick() call exists outside the If statement
        kick_outside_if = False
        for stmt in func_def.body:
            if stmt is not sentinel_if:
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Attribute) and \
                           node.func.attr == "kick":
                            kick_outside_if = True
                            break
                if kick_outside_if:
                    break

        assert not kick_outside_if, \
            "kick() call found outside the If statement " \
            "(should only be inside ROUTING_UNAVAILABLE conditional)"


# ---------------------------------------------------------------------------
# Reconcile pairing — reload scope + the 5-mutation lifecycle sequencer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReloadScopeReconcilesEvents:
    async def test_reload_triggers_scope_reconciles_events(self, monkeypatch):
        import reload as reload_mod
        import trigger_reconcile
        import callback_reconcile
        import event_reconcile

        async def _fake_handler(runtime, *, role=None):
            return ["reloaded"]

        monkeypatch.setitem(reload_mod._HANDLERS, "triggers", _fake_handler)
        tg = AsyncMock(return_value=[])
        cb = AsyncMock(return_value=[])
        ev = AsyncMock(return_value=[])
        monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime", tg)
        monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime", cb)
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", ev)

        runtime = SimpleNamespace(trigger_registry=object())
        res = await reload_mod.dispatch("triggers", runtime=runtime, role="gmail")
        assert res["status"] == "ok"
        ev.assert_awaited_once()
        assert ev.await_args.args[0] is runtime
        assert "plugin_events_reconciled" in res["actions"]

    async def test_reload_event_reconcile_failure_is_non_fatal(self, monkeypatch):
        import reload as reload_mod
        import trigger_reconcile
        import callback_reconcile
        import event_reconcile

        async def _fake_handler(runtime, *, role=None):
            return []

        monkeypatch.setitem(reload_mod._HANDLERS, "agent", _fake_handler)
        monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))
        monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))

        async def _boom(runtime, **kw):
            raise RuntimeError("event reconcile blew up")

        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", _boom)
        runtime = SimpleNamespace(trigger_registry=object())
        res = await reload_mod.dispatch("agent", runtime=runtime, role="gmail")
        assert res["status"] == "ok"
        assert "plugin_events_reconciled" not in res["actions"]


@pytest.mark.asyncio
class TestLifecycleSequencerReconcilesEvents:
    async def test_mutation_sequencer_reconciles_events(self, monkeypatch):
        """tools._reload_and_verify_targets — the choke point all 5
        lifecycle mutations funnel through — reconciles events alongside
        triggers/callbacks, passing the SAME runtime."""
        import agent as agent_mod
        import plugin_registry
        import tools as tools_mod
        import event_reconcile

        monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda: None)
        monkeypatch.setattr(plugin_registry, "snapshot_generation", lambda: 1)
        runtime = SimpleNamespace(trigger_registry=object())
        monkeypatch.setattr(agent_mod, "active_runtime", runtime)
        monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                            lambda plugin_name: {"ready": True, "targets": []})
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: None)

        async def _fake_notify():
            return None

        monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible",
                            _fake_notify)

        import trigger_reconcile
        import callback_reconcile
        monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))
        monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))
        ev = AsyncMock(return_value=[])
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", ev)

        result = await tools_mod._reload_and_verify_targets(
            "p", [], expect="present")
        assert result["ok"] is True
        ev.assert_awaited_once()
        assert ev.await_args.args[0] is runtime

    async def test_mutation_sequencer_survives_event_reconcile_failure(
            self, monkeypatch):
        import agent as agent_mod
        import plugin_registry
        import tools as tools_mod
        import event_reconcile

        monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda: None)
        monkeypatch.setattr(plugin_registry, "snapshot_generation", lambda: 1)
        monkeypatch.setattr(agent_mod, "active_runtime", None)
        monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                            lambda plugin_name: {"ready": True, "targets": []})
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: None)

        async def _fake_notify():
            return None

        monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible",
                            _fake_notify)

        async def _boom(runtime, **kw):
            raise RuntimeError("event reconcile blew up")

        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", _boom)
        result = await tools_mod._reload_and_verify_targets(
            "p", [], expect="present")
        assert result["ok"] is True  # never fails the mutation


# ---------------------------------------------------------------------------
# Health merge — event_reconcile.current_issues() folds in as dicts.
# ---------------------------------------------------------------------------


def _issue(name, reason_code):
    return {"name": name, "target": None, "stage": "events",
            "reason_code": reason_code, "artifact_id": "art-1"}


class TestHealthMergeIncludesEventIssues:
    def test_event_issues_land_in_the_health_report(self, tmp_path, monkeypatch):
        import tools
        import event_reconcile
        import trigger_reconcile
        import callback_reconcile
        import plugin_registry

        monkeypatch.setattr(tools, "_PLUGIN_HEALTH_PATH", tmp_path / "health.json")
        monkeypatch.setattr(trigger_reconcile, "current_issues", lambda: [])
        monkeypatch.setattr(callback_reconcile, "current_issues", lambda: [])
        monkeypatch.setattr(
            event_reconcile, "current_issues",
            lambda: [_issue("finance", "event_pending_ack")])
        monkeypatch.setattr(
            plugin_registry, "resolve_all",
            lambda: SimpleNamespace(issues=[], warnings=[]))
        monkeypatch.setattr(
            plugin_registry, "load_registry",
            lambda *a, **k: SimpleNamespace(valid=False, entries=[]))

        written = {}
        import plugin_health
        monkeypatch.setattr(
            plugin_health, "write_report", lambda **kw: written.update(kw))

        tools._regenerate_plugin_health([])
        codes = {i.get("reason_code") if isinstance(i, dict)
                 else getattr(i, "reason_code", None)
                 for i in written["issues"]}
        assert "event_pending_ack" in codes

    def test_event_issues_are_dicts_never_plugin_issue_instances(self):
        """Minor-10's own contract: current_issues() rows are concatenated
        directly, never routed through the PluginIssue-attribute-only
        _add()/_rediscoverable() helpers."""
        import tools
        import inspect
        src = inspect.getsource(tools._regenerate_plugin_health)
        assert "event_reconcile.current_issues()" in src
        assert "list(event_issues)" in src

    def test_previously_flagged_regression_tests_stay_green(self):
        """Regression pin for the 4 tests a naive direct-concat broke
        (test_verify_plugin_state.py x3, test_plugin_triggers_reconcile.py
        x1): they all call _regenerate_plugin_health WITHOUT mocking
        event_reconcile, so the conftest-level _reset_event_routing fixture
        (an authoritative empty routing map, never the process-default
        ROUTING_UNAVAILABLE sentinel) is what keeps them green. This test
        merely proves the fixture exists and does what it claims."""
        import event_reconcile
        import event_spool
        # Under the autouse conftest fixture, an untouched test sees an
        # authoritative empty map, never the sentinel.
        assert event_reconcile.get_routed() == {}
        assert event_reconcile.get_routed() is not event_spool.ROUTING_UNAVAILABLE
        assert event_reconcile.current_issues() == []


# ---------------------------------------------------------------------------
# Fail-closed registry — get_installed/get_registry_valid.
# ---------------------------------------------------------------------------


class TestFailClosedRegistry:
    def test_registry_valid_true_reports_membership(self, monkeypatch):
        import casa_core
        import plugin_registry
        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(
                valid=True, entries=[{"name": "gmail"}, {"name": "finance"}]))
        assert casa_core._event_registry_valid() is True
        assert casa_core._event_installed() == {"gmail", "finance"}

    def test_invalid_registry_never_fabricates_installed_set(self, monkeypatch):
        """The core fail-closed contract: an invalid snapshot must never
        look like "nothing is installed" in a way that would license the
        worker's sweep to vaporize every subscriber's spool state — it must
        report NO membership at all, gated by get_registry_valid()==False."""
        import casa_core
        import plugin_registry
        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(
                valid=False, entries=[{"name": "ghost"}]))
        assert casa_core._event_registry_valid() is False
        assert casa_core._event_installed() == set()

    def test_worker_pass_does_no_destructive_work_under_invalid_registry(
            self, monkeypatch, tmp_path):
        """Integration-level: wiring event_episodes with THESE fail-closed
        closures against a real spool with an orphaned emitter dir must
        never GC it while the registry is invalid — the destructive
        decision itself is event_spool's own tested contract; this proves
        casa_core's closures feed it the fail-closed inputs."""
        import asyncio
        import event_episodes
        import event_spool
        import event_reconcile
        import casa_core
        import plugin_registry

        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(valid=False, entries=[]))
        spool = event_spool.EventSpool(tmp_path / "events")
        try:
            emitter_dir = spool.root / "ghost"
            emitter_dir.mkdir(parents=True, exist_ok=True)

            monkeypatch.setattr(event_episodes, "_dispatch", None)
            monkeypatch.setattr(event_episodes, "_resolve_registry_entry", None)
            monkeypatch.setattr(event_episodes, "_get_routed",
                                lambda: event_spool.ROUTING_UNAVAILABLE)
            monkeypatch.setattr(event_episodes, "_get_installed",
                                casa_core._event_installed)
            monkeypatch.setattr(event_episodes, "_get_registry_valid",
                                casa_core._event_registry_valid)
            monkeypatch.setattr(event_episodes, "_get_acks", None)
            monkeypatch.setattr(event_episodes, "_get_spool", lambda: spool)
            monkeypatch.setattr(event_episodes, "_notify_operator", None)

            asyncio.run(event_episodes.recovery(boot=True))
            assert emitter_dir.is_dir(), \
                "an invalid registry must leave spool dirs untouched"
        finally:
            spool.close()


# ---------------------------------------------------------------------------
# event_wake is casa-internal only.
# ---------------------------------------------------------------------------


class TestEventWakeMarkerUnreachable:
    def test_synthetic_key_is_reserved(self):
        from provenance import RESERVED_CONTEXT_KEYS
        assert "synthetic" in RESERVED_CONTEXT_KEYS

    def test_event_wake_value_is_stripped_from_external_context(self):
        """An external caller (webhook payload, /invoke body) cannot forge
        the event_wake marker a casa-composed nudge carries — sanitize_
        external_context strips the WHOLE synthetic key regardless of the
        value an attacker sets it to."""
        from provenance import sanitize_external_context
        forged = {"synthetic": "event_wake", "emitter": "gmail",
                  "event": "new_mail", "other": "kept"}
        out = sanitize_external_context(forged)
        assert "synthetic" not in out
        # only the RESERVED key is stripped — everything else passes
        # through untouched (proves this is a targeted strip, not a wipe).
        assert out == {"emitter": "gmail", "event": "new_mail",
                       "other": "kept"}

    def test_wake_context_shape_matches_the_reserved_marker(self):
        """event_episodes._wake_context produces exactly the marker
        provenance.py reserves — proving the two modules agree on the
        literal value, not just that SOME key is reserved."""
        import event_episodes
        ctx = event_episodes._wake_context("gmail", "new_mail")
        assert ctx["synthetic"] == "event_wake"
        from provenance import RESERVED_CONTEXT_KEYS
        assert set(ctx) <= RESERVED_CONTEXT_KEYS | {"emitter", "event"}
        assert "synthetic" in RESERVED_CONTEXT_KEYS


# ---------------------------------------------------------------------------
# ask_user provenance rejection on an event-dispatched turn.
# ---------------------------------------------------------------------------


def _set_origin(agent_mod, **overrides):
    origin = {
        "role": "assistant",
        "channel": "telegram",
        "chat_id": "500",
        "user_id": 999,
        "message_type": "channel_in",
        "source": "telegram",
        "execution_role": "assistant",
    }
    origin.update(overrides)
    return agent_mod.origin_var.set(origin)


@pytest.mark.asyncio
class TestAskUserRejectsEventDispatchedTurn:
    async def test_ask_user_rejects_event_wake_origin(self, monkeypatch):
        """tools.py:599 — the real provenance classifier, driven with a
        synthetic event_wake context exactly as event_episodes._wake_context
        composes it, must reject ask_user (an event nudge is headless: 'do
        not ask' per its own instruction wording — this is the load-bearing
        enforcement of that rule)."""
        import agent as agent_mod
        import tools as tools_mod
        from unittest.mock import MagicMock

        tools_mod.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        )
        tok = _set_origin(agent_mod, synthetic="event_wake",
                          emitter="gmail", event="new_mail")
        try:
            res = await tools_mod.ask_user.handler(
                {"question": "Proceed?", "options": ["Yes", "No"]})
        finally:
            agent_mod.origin_var.reset(tok)
        import json
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
