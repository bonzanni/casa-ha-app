"""v0.146.0 — at-least-once idempotent delivery nudge.

Modeled on ``plugin_setup_episodes``: a durable ``(plugin, result_hash)``
ledger, a supervised worker, crash-safe recording and boot recovery. The
worker is driven directly in tests (``_worker_pass``/``recovery``) with an
injected ``sleep`` — never by patching a global ``asyncio.sleep`` — exactly
as ``tests/test_plugin_setup_episodes.py`` drives its worker.
"""

from __future__ import annotations

import pytest

import callback_episodes as ce


PLUGIN = "demo"
HASH = "a" * 64            # sha256(state) hex — the result handle
HASH2 = "b" * 64


class FakeSpool:
    """Minimal stand-in for ``CallbackSpool`` — only the three read methods
    the delivery nudge uses (``plugins``/``list_results``/``has_result``)."""

    def __init__(self) -> None:
        self.results: dict[str, set[str]] = {}

    def add(self, plugin: str, h: str) -> None:
        self.results.setdefault(plugin, set()).add(h)

    def drop(self, plugin: str, h: str) -> None:
        self.results.get(plugin, set()).discard(h)

    def plugins(self) -> list[str]:
        return sorted(self.results)

    def list_results(self, plugin: str) -> list[str]:
        return sorted(self.results.get(plugin, set()))

    def has_result(self, plugin: str, h: str) -> bool:
        return h in self.results.get(plugin, set())


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "STORE_PATH", tmp_path / "callback-episodes.json")
    monkeypatch.setattr(ce, "_worker_task", None)
    monkeypatch.setattr(ce, "_lock", None)
    monkeypatch.setattr(ce, "_kick", None)
    ce._pending_hints.clear()

    spool = FakeSpool()
    state = {
        "spool": spool,
        "entry": {"targets": ["resident:assistant"]},
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

    ce.configure(
        dispatch=dispatch,
        resolve_registry_entry=lambda plugin: state["entry"],
        get_spool=lambda: spool,
        notify_operator=notify,
        sleep=fake_sleep,
    )
    return state


# ---------------------------------------------------------------------------
# kick — non-durable, O(1), no file I/O on the request path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kick_then_worker_records_and_dispatches_once(wired):
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    assert len(wired["dispatches"]) == 1
    eps = ce.episodes()
    assert len(eps) == 1
    assert eps[0]["plugin"] == PLUGIN
    assert eps[0]["result_hash"] == HASH
    assert eps[0]["status"] == "dispatched"


def test_kick_performs_no_file_io(wired, monkeypatch):
    # Request-path O(1): kick must touch neither _load nor _save.
    def _boom(*a, **k):
        raise AssertionError("kick touched the durable store")

    monkeypatch.setattr(ce, "_save", _boom)
    monkeypatch.setattr(ce, "_load", _boom)
    ce.kick(PLUGIN, HASH)
    assert (PLUGIN, HASH) in ce._pending_hints
    assert ce._kick is not None and ce._kick.is_set()


# ---------------------------------------------------------------------------
# idempotent enqueue + tombstones
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotent_enqueue_same_key_once(wired):
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce.recovery(wired["spool"])          # scan also enqueues
    assert len(ce.episodes()) == 1


@pytest.mark.asyncio
async def test_tombstone_blocks_reenqueue_while_result_exists(wired):
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()                     # dispatched + tombstone written
    assert any(t["plugin"] == PLUGIN and t["result_hash"] == HASH
               for t in ce._load()["tombstones"])
    # A fresh recovery pass while the result still lingers must not re-enqueue.
    await ce.recovery(wired["spool"])
    assert len(wired["dispatches"]) == 1
    assert len([e for e in ce.episodes() if e["status"] == "pending"]) == 0


@pytest.mark.asyncio
async def test_tombstone_pruned_after_result_gone_allows_new(wired):
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    assert len(wired["dispatches"]) == 1
    # Consumer collected it / TTL expired — the result file is gone.
    wired["spool"].drop(PLUGIN, HASH)
    await ce._worker_pass()                     # settle: prune episode+tombstone
    assert ce._load()["tombstones"] == []
    assert ce.episodes() == []
    # A brand-new result reusing the same hash re-enqueues and re-dispatches.
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    assert len(wired["dispatches"]) == 2


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_enqueues_result_without_episode(wired):
    # A result published while the worker was down (no kick recorded).
    wired["spool"].add(PLUGIN, HASH)
    await ce.recovery(wired["spool"])
    eps = ce.episodes("pending")
    assert len(eps) == 1
    assert eps[0]["result_hash"] == HASH


@pytest.mark.asyncio
async def test_pending_dropped_when_result_absent(wired):
    # A pending episode whose result expired before the nudge fired is dropped
    # (the credential is dead — nudging collects nothing).
    wired["spool"].add(PLUGIN, HASH)
    await ce.recovery(wired["spool"])
    assert len(ce.episodes("pending")) == 1
    wired["spool"].drop(PLUGIN, HASH)
    wired["dispatch_ok"] = True
    await ce._worker_pass()
    assert ce.episodes() == []
    assert wired["dispatches"] == []


# ---------------------------------------------------------------------------
# at-least-once redelivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_at_least_once_redelivery_on_lost_dispatch_mark(wired):
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    assert len(wired["dispatches"]) == 1
    # Crash BETWEEN bus-accept and the durable dispatched mark: the mark (and
    # its tombstone) never persisted, so on restart the episode is pending.
    data = ce._load()
    for e in data["episodes"]:
        e["status"] = "pending"
    data["tombstones"] = []
    ce._save(data)
    await ce._worker_pass()
    # Redelivered — at-least-once, not exactly-once (documented, idempotent
    # for the consumer: a second collect against an emptied dir is a no-op).
    assert len(wired["dispatches"]) == 2


# ---------------------------------------------------------------------------
# target selection (copied verbatim from plugin_setup_episodes._compose)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_target_prefers_assistant_when_present(wired):
    wired["entry"] = {"targets": ["resident:zeta", "resident:assistant"]}
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    role, text, ctx = wired["dispatches"][0]
    assert role == "assistant"
    assert ctx["synthetic"] == "callback_nudge"


@pytest.mark.asyncio
async def test_target_first_sorted_resident_fallback(wired):
    wired["entry"] = {"targets": ["resident:mars", "resident:aqua"]}
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    role, _text, _ctx = wired["dispatches"][0]
    assert role == "aqua"


@pytest.mark.asyncio
async def test_specialist_only_delegates_via_assistant(wired):
    wired["entry"] = {"targets": ["specialist:finance"]}
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    role, text, _ctx = wired["dispatches"][0]
    assert role == "assistant"
    assert "'finance'" in text
    assert "Delegate" in text


@pytest.mark.asyncio
async def test_no_target_fails_and_notes(wired):
    wired["entry"] = {"targets": []}
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    assert wired["dispatches"] == []
    ep = ce.episodes()[0]
    assert ep["status"] == "failed"
    assert wired["notes"]


# ---------------------------------------------------------------------------
# message wording (fixed casa-authored turn)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nudge_message_is_the_fixed_wording(wired):
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    _role, text, _ctx = wired["dispatches"][0]
    assert (f"Authorization result for '{PLUGIN}' is waiting (handle {HASH}) "
            "— collect it now.") in text


# ---------------------------------------------------------------------------
# dispatch failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_failure_leaves_pending_for_retry(wired):
    wired["dispatch_ok"] = False
    wired["spool"].add(PLUGIN, HASH)
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    ep = ce.episodes()[0]
    assert ep["status"] == "pending"            # not tombstoned — retried
    assert ce._load()["tombstones"] == []
    # Bus recovers: the next pass delivers and marks dispatched.
    wired["dispatch_ok"] = True
    await ce._worker_pass()
    assert wired["dispatches"][-1][0] == "assistant"
    assert ce.episodes()[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_unresolved_registry_keeps_pending(wired):
    # Registry not yet resolvable (transient) — the episode stays pending and
    # retries on a later kick rather than being lost or marked failed.
    wired["spool"].add(PLUGIN, HASH)
    wired["entry"] = None
    ce.kick(PLUGIN, HASH)
    await ce._worker_pass()
    assert wired["dispatches"] == []
    assert ce.episodes()[0]["status"] == "pending"
    assert ce._load()["tombstones"] == []
