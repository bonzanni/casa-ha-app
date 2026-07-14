"""W3 (v0.77.0): turn-owned bounded typing leases + button-continuation start.

Sol r1-1: leases are keyed by the turn's cid so overlapping same-chat turns
(the fast-tap timeline — the original turn still narrating while the
button-continuation turn starts) don't cancel each other's indicator.
Sol r1-2: every lease carries a TTL the loop itself enforces, so an
accepted-but-never-consumed turn leaks at most one lease-TTL of "typing…"
even when no teardown ever runs.

Never patch the global ``asyncio.sleep`` (repo hard rule). These tests bound
the loop via short per-lease TTLs and the module-local ``_TYPING_INTERVAL``
constant, and use plain async coroutines (not AsyncMock) for the chat-action
send so the loop's call history can't accumulate unbounded.
"""

from __future__ import annotations

import asyncio

import pytest

import channels.telegram as tg
from channels.telegram import TelegramChannel

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _channel() -> TelegramChannel:
    return TelegramChannel(
        bot_token="t", chat_id="1", default_agent="a", delivery_mode="block",
    )


class _CountingBot:
    """A bot whose send_chat_action is a plain coroutine (no AsyncMock
    accumulation) that counts invocations."""

    def __init__(self) -> None:
        self.actions = 0

    async def send_chat_action(self, **_kw) -> None:
        self.actions += 1


def _install_bot(ch: TelegramChannel) -> _CountingBot:
    bot = _CountingBot()
    ch._app = type("_App", (), {"bot": bot})()
    return bot


# --------------------------------------------------------------------------
# (a) OVERLAP — original + continuation leases coexist; each turn releases
#     only its own lease; the loop survives until the LAST lease is gone.
# --------------------------------------------------------------------------


async def test_overlap_original_release_keeps_continuation_lease_alive():
    ch = _channel()
    _install_bot(ch)

    ch._start_typing("1", "cidA")  # original turn
    ch._start_typing("1", "cidB")  # button continuation (same chat)

    loop_task = ch._typing_loops["1"]
    assert set(ch._typing_leases["1"]) == {"cidA", "cidB"}
    assert not loop_task.done()

    # Original turn finishes and releases ITS lease by cid (r1-1) — the
    # continuation's indicator must survive.
    await ch.turn_finished({"chat_id": "1", "cid": "cidA"})
    await asyncio.sleep(0)
    assert set(ch._typing_leases["1"]) == {"cidB"}
    assert ch._typing_loops.get("1") is loop_task
    assert not loop_task.done()

    # Continuation delivers and releases its lease → loop exits.
    await ch.turn_finished({"chat_id": "1", "cid": "cidB"})
    await asyncio.sleep(0)
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops
    assert loop_task.done()


# --------------------------------------------------------------------------
# (b) accepted-but-never-consumed — no teardown ever runs; the lease expires
#     at TTL, the loop exits WITHOUT being cancelled, and nothing leaks.
# --------------------------------------------------------------------------


async def test_lease_expires_at_ttl_loop_exits_without_cancel(monkeypatch):
    # Fast passes via the module-local interval constant (NOT asyncio.sleep).
    monkeypatch.setattr(tg, "_TYPING_INTERVAL", 0.0)
    ch = _channel()
    bot = _install_bot(ch)

    ch._start_typing("1", "orphan", ttl_s=0.05)
    loop_task = ch._typing_loops["1"]

    # No teardown is ever called — the loop must self-bound on the TTL.
    await asyncio.wait_for(loop_task, timeout=2.0)

    assert loop_task.done()
    assert loop_task.cancelled() is False  # natural exit, not a cancel
    assert bot.actions >= 1  # it actually ran before expiring
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


# --------------------------------------------------------------------------
# (c) idempotent double-release; release-all fallback when no cid is present.
# --------------------------------------------------------------------------


async def test_double_release_is_idempotent():
    ch = _channel()
    _install_bot(ch)
    ch._start_typing("1", "cidA")

    ch._stop_typing("1", "cidA")
    ch._stop_typing("1", "cidA")  # second release must be a no-op, not raise
    ch._stop_typing("1", "never-existed")  # unknown lease id — no-op

    await asyncio.sleep(0)
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


async def test_release_all_fallback_when_context_lacks_cid():
    ch = _channel()
    _install_bot(ch)
    ch._start_typing("1", "cidA")
    ch._start_typing("1", "cidB")
    loop_task = ch._typing_loops["1"]

    # A legacy delivery context with no cid → release ALL leases for the chat
    # (preserves today's semantics).
    await ch.turn_finished({"chat_id": "1"})
    await asyncio.sleep(0)

    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops
    assert loop_task.done()


async def test_start_typing_noop_when_suspended():
    ch = _channel()
    _install_bot(ch)
    ch._typing_suspended = True
    ch._start_typing("1", "cidA")
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


# --------------------------------------------------------------------------
# (d) button continuation — lease started ONLY on acceptance, keyed by the
#     cid the dispatched turn carries; no lease on exhaustion / cancel.
# --------------------------------------------------------------------------


class _Bus:
    def __init__(self, results):
        self._results = list(results)
        self.sent = []

    async def send_checked(self, msg):
        self.sent.append(msg)
        result = self._results.pop(0)
        if isinstance(result, BaseException):  # incl. CancelledError
            raise result
        return result


async def test_dispatch_success_starts_lease_keyed_by_turn_cid():
    ch = _channel()
    bus = _Bus(["accepted"])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    ok = await ch._dispatch_button_continuation(
        chat_id=1, user_id=2, target_role="ellen",
        request_id="req-1", text="approve",
    )

    assert ok is True
    # The lease id MUST equal the cid the dispatched (accepted) turn carries.
    dispatched_cid = bus.sent[-1].context["cid"]
    assert started == [("1", dispatched_cid)]


async def test_dispatch_exhaustion_starts_no_lease():
    ch = _channel()
    bus = _Bus(["no_target", "no_target", "no_target"])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    ok = await ch._dispatch_button_continuation(
        chat_id=1, user_id=2, target_role="ellen",
        request_id="req-1", text="approve",
        _sleep=lambda _d: asyncio.sleep(0),
    )

    assert ok is False
    assert started == []


async def test_dispatch_cancel_starts_no_lease():
    ch = _channel()
    bus = _Bus([asyncio.CancelledError()])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    with pytest.raises(asyncio.CancelledError):
        await ch._dispatch_button_continuation(
            chat_id=1, user_id=2, target_role="ellen",
            request_id="req-1", text="approve",
        )

    assert started == []


async def test_dispatch_reuses_one_cid_across_retries():
    """The cid must be stable across retries so the lease id (started after a
    late acceptance) still equals the accepted turn's cid."""
    ch = _channel()
    bus = _Bus(["no_target", "accepted"])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    ok = await ch._dispatch_button_continuation(
        chat_id=1, user_id=2, target_role="ellen",
        request_id="req-1", text="approve",
        _sleep=lambda _d: asyncio.sleep(0),
    )

    assert ok is True
    cids = {m.context["cid"] for m in bus.sent}
    assert len(cids) == 1  # same cid on both attempts
    assert started == [("1", bus.sent[-1].context["cid"])]
