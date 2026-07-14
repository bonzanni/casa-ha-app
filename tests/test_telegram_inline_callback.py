"""Tests for TelegramChannel inline-callback dispatch.

v0.75.0 (W5/Sol B3,B4): the callback ``_on_inline_callback`` was rewritten
onto ``verdict_broker.BROKER`` with a fail-closed contract — parse
``v1|ns|rid|idx`` (legacy ``perm:<verdict>:<rid>`` still routes), fail
closed on a missing/anonymous/wrong actor, reject a TERMINAL engagement
BEFORE claiming, claim/commit exactly once, and EXACTLY ONE
``await cq.answer(toast)`` per path. The callback itself never edits the
keyboard — that's the broker finish-hook's job (covered in
``tests/test_hooks_engagement_permission_relay.py``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    """Isolate every test on its own VerdictBroker — telegram.py resolves
    ``from verdict_broker import BROKER`` at call time inside
    ``_on_inline_callback``, so redirecting the module attribute here is
    picked up transparently."""
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


def _mk_callback_update(*, data, thread_id, chat_id, query_id="cq1",
                         user_id=999, answer_side_effect=None):
    cq = SimpleNamespace(
        id=query_id,
        data=data,
        message=SimpleNamespace(
            message_thread_id=thread_id,
            chat=SimpleNamespace(id=chat_id),
        ),
        answer=AsyncMock(return_value=None, side_effect=answer_side_effect),
        from_user=(SimpleNamespace(id=user_id) if user_id is not None else None),
    )
    return SimpleNamespace(callback_query=cq)


def _seed(broker, *, ns="permission", scope, rid, topic_id, operator_id,
          options=("allow", "deny"), timeout_s=5.0):
    req, created = broker.register(
        namespace=ns, scope=scope, request_id=rid, timeout_s=timeout_s,
    )
    assert created is True
    req.meta.update({
        "options": list(options), "topic_id": topic_id,
        "operator_id": operator_id,
    })
    return req


def _mk_channel(fake_telegram_bot, engagement_fixture):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(
        bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
    )
    ch._engagement_registry = engagement_fixture.registry
    return ch


class TestV1AndLegacyRouting:
    async def test_v1_permission_allow_routes_and_answers_tick(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-001",
                   topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-001|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome == {"outcome": "answered", "option_index": 0,
                           "actor_id": 999}

    async def test_v1_permission_deny_routes_and_answers_tick(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-002",
                   topic_id=rec.topic_id, operator_id=42)

        update = _mk_callback_update(
            data="v1|permission|rid-002|1", thread_id=rec.topic_id,
            chat_id=-1001, user_id=42,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["option_index"] == 1

    async def test_legacy_perm_allow_still_routes(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-legacy",
                   topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="perm:allow:rid-legacy", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome == {"outcome": "answered", "option_index": 0,
                           "actor_id": 999}

    async def test_legacy_perm_deny_still_routes(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-legacy-d",
                   topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="perm:deny:rid-legacy-d", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["option_index"] == 1


class TestFailClosedActorBinding:
    async def test_wrong_user_refused_without_claiming(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-wu",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-wu|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=42,  # not the bound operator
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("not for you")
        # No claim: request still live/unresolved.
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-wu",
        ]

    async def test_missing_operator_id_fails_closed(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-noop",
              topic_id=rec.topic_id, operator_id=None)

        update = _mk_callback_update(
            data="v1|permission|rid-noop|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("not for you")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-noop",
        ]

    async def test_from_user_none_fails_closed(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-anon",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-anon|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=None,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-anon",
        ]


class TestTerminalRecordRejection:
    async def test_terminal_record_rejected_before_claim(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-term",
              topic_id=rec.topic_id, operator_id=999)
        rec.status = "completed"  # simulate a terminal record

        update = _mk_callback_update(
            data="v1|permission|rid-term|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-term",
        ]

    async def test_terminal_flip_barrier_rejects_mid_flight(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r7-B6: try_transition_terminal flips rec.status synchronously
        BEFORE the (awaited) tombstone write completes. A tap arriving in
        that window must be rejected — closes the race where
        _finalize_engagement hasn't reached cancel_scope yet."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-barrier",
              topic_id=rec.topic_id, operator_id=999)

        gate = asyncio.Event()
        orig_write_locked = registry._write_tombstone_locked

        async def _gated_write_locked():
            await gate.wait()
            await orig_write_locked()

        registry._write_tombstone_locked = _gated_write_locked

        flip_task = asyncio.create_task(
            registry.try_transition_terminal(rec.id, "completed", completed_at=1.0)
        )
        for _ in range(200):
            if rec.status == "completed":
                break
            await asyncio.sleep(0)
        assert rec.status == "completed"
        assert not flip_task.done()  # tombstone I/O still pending

        update = _mk_callback_update(
            data="v1|permission|rid-barrier|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-barrier",
        ]

        gate.set()
        await flip_task


class TestMetaMismatchRejection:
    async def test_wrong_topic_rejected(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-wt",
              topic_id=999999, operator_id=999)  # meta says a DIFFERENT topic

        update = _mk_callback_update(
            data="v1|permission|rid-wt|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("expired")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-wt",
        ]

    async def test_out_of_range_option_index_rejected(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-oor",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-oor|5", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with("invalid")
        assert _fresh_broker.pending(namespace="permission", scope=rec.id) == [
            "rid-oor",
        ]

    async def test_no_live_meta_is_expired(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        update = _mk_callback_update(
            data="v1|permission|no-such-rid|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")


class TestMalformedCallbackData:
    async def test_malformed_shapes_all_expire(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        for data in (
            "not-perm:x:y",
            "perm:nope:rid",
            "perm:allow",
            "v1|unknown_ns|rid|0",
            "v1|permission|rid|notanint",
            "v1|permission|rid",
            "v1|permission||0",
            "x" * 100,
        ):
            update = _mk_callback_update(
                data=data, thread_id=rec.topic_id, chat_id=-1001,
            )
            await ch._on_inline_callback(update, context=None)
            update.callback_query.answer.assert_awaited_once_with("expired")

    async def test_unknown_topic_id_is_expired(
        self, fake_telegram_bot, engagement_fixture,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        update = _mk_callback_update(
            data="v1|permission|rid-1|0", thread_id=99999, chat_id=-1001,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")


class TestClaimCommitAndDuplicates:
    async def test_duplicate_tap_after_winner_is_already_answered(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-dup",
              topic_id=rec.topic_id, operator_id=999)

        update1 = _mk_callback_update(
            data="v1|permission|rid-dup|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update1, context=None)
        update1.callback_query.answer.assert_awaited_once_with("✔")

        update2 = _mk_callback_update(
            data="v1|permission|rid-dup|1", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update2, context=None)
        update2.callback_query.answer.assert_awaited_once_with("already answered")

    async def test_stale_tap_after_timeout_is_expired(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        req = _seed(_fresh_broker, scope=rec.id, rid="rid-stale",
                   topic_id=rec.topic_id, operator_id=999, timeout_s=0.05)
        await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)  # times out

        update = _mk_callback_update(
            data="v1|permission|rid-stale|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")

    async def test_answer_raising_does_not_crash_handler(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-boom",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|permission|rid-boom|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
            answer_side_effect=RuntimeError("telegram transport down"),
        )
        await ch._on_inline_callback(update, context=None)  # must not raise
        update.callback_query.answer.assert_awaited_once_with("✔")


class TestEngagementAskStateAdvance:
    """v0.75.0 (Task 2): the engagement_ask branch is wired generically —
    advance_interaction_state doesn't exist until Task 7, so a registry
    lacking it takes the no-op skip-to-commit path. These tests drive the
    branch that WILL activate once Task 7 lands the method."""

    async def test_state_write_failure_aborts_claim_and_stays_live(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        registry = engagement_fixture.registry
        registry.advance_interaction_state = AsyncMock(
            side_effect=RuntimeError("db down"),
        )
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-1",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-1|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with(
            "couldn't record — please tap again",
        )
        registry.advance_interaction_state.assert_awaited_once_with(
            rec.id, "operator_answered",
        )
        # NOT resolved: claim was aborted, request stays live for a re-tap.
        assert _fresh_broker.pending(namespace="engagement_ask", scope=rec.id) == [
            "ask-1",
        ]

        # A re-tap can still win (claim wasn't stranded).
        registry.advance_interaction_state = AsyncMock(return_value=None)
        update2 = _mk_callback_update(
            data="v1|engagement_ask|ask-1|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update2, context=None)
        update2.callback_query.answer.assert_awaited_once_with("✔")

    async def test_real_registry_persist_failure_aborts_claim_and_stays_live(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker, monkeypatch,
    ):
        """B3 (Sol r1): end-to-end with the REAL registry — a genuine
        tombstone-write failure (underlying file write raises) makes
        advance_interaction_state raise, so the callback aborts the claim,
        tells the operator to re-tap, and leaves the request live. The
        in-memory interaction_state is rolled back (never left advanced)."""
        import engagement_registry as er

        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        # A real interaction-required engagement whose ask is pending.
        rec.interaction_state = "first_contact_required"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-b3",
              topic_id=rec.topic_id, operator_id=999)

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(er, "atomic_write_json", _boom)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-b3|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        update.callback_query.answer.assert_awaited_once_with(
            "couldn't record — please tap again",
        )
        # Request still live (claim aborted); state rolled back, not advanced.
        assert _fresh_broker.pending(
            namespace="engagement_ask", scope=rec.id,
        ) == ["ask-b3"]
        assert rec.interaction_state == "first_contact_required"

    async def test_real_registry_noninteraction_required_still_commits(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """Task 7 activates the seam: the real EngagementRegistry now HAS
        advance_interaction_state. For a non-interaction-required
        engagement (interaction_state == "", the default), the pure
        transition is a no-op (returns None, no exception) — the tap
        still commits and the state stays untouched."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        assert hasattr(engagement_fixture.registry, "advance_interaction_state")
        assert rec.interaction_state == ""
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                   rid="ask-2", topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-2|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("✔")
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.1)
        assert outcome["outcome"] == "answered"
        assert rec.interaction_state == ""

    async def test_cancelled_after_claim_releases_it(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r7-B1: any exit without a commit — including CancelledError,
        which `except Exception` does NOT catch — must abort_claim so the
        request isn't stranded with a cancelled timer."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-cxl",
              topic_id=rec.topic_id, operator_id=999)

        def _raise_cancelled(claim):
            raise asyncio.CancelledError()

        # Instance-attribute override shadows the bound class method for
        # this broker instance only; `del` below restores normal lookup.
        _fresh_broker.commit = _raise_cancelled
        try:
            update = _mk_callback_update(
                data="v1|permission|rid-cxl|0", thread_id=rec.topic_id,
                chat_id=-1001, user_id=999,
            )
            with pytest.raises(asyncio.CancelledError):
                await ch._on_inline_callback(update, context=None)
        finally:
            del _fresh_broker.commit  # restore the bound class method

        # The claim was released (abort_claim re-armed the timer) — a fresh
        # claim on the SAME request now succeeds instead of "duplicate".
        claim = _fresh_broker.claim(
            namespace="permission", scope=rec.id, request_id="rid-cxl",
            option_index=1, actor_id=999,
        )
        assert not isinstance(claim, str)


class TestInteractionStateActivation:
    """W2/Sol B9 (Task 7): the real EngagementRegistry now carries
    ``interaction_state`` + ``advance_interaction_state`` — these drive the
    callback against a record seeded ``awaiting_operator`` directly (per
    the brief: "your registry tests can set the field directly")."""

    async def test_winning_tap_authorizes_before_commit_resolves(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """B9/r2-B7: ordered event list — claim -> advance_interaction_state
        -> commit — so the state is ALREADY authorized at the moment commit
        resolves the awaiting handler."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-order",
              topic_id=rec.topic_id, operator_id=999)

        seen_state_at_commit: list[str] = []
        orig_commit = _fresh_broker.commit

        def _spy_commit(claim):
            seen_state_at_commit.append(rec.interaction_state)
            return orig_commit(claim)

        _fresh_broker.commit = _spy_commit
        try:
            update = _mk_callback_update(
                data="v1|engagement_ask|ask-order|0", thread_id=rec.topic_id,
                chat_id=-1001, user_id=999,
            )
            await ch._on_inline_callback(update, context=None)
        finally:
            del _fresh_broker.commit

        assert seen_state_at_commit == ["authorized"]
        assert rec.interaction_state == "authorized"
        update.callback_query.answer.assert_awaited_once_with("✔")

    async def test_fast_tap_wins_and_authorizes_from_first_contact_required(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r3-B4: a tap that beats the agent's first `reply` (state still
        ``first_contact_required``) still wins the claim and authorizes
        directly — never left stuck awaiting."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "first_contact_required"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-fast",
              topic_id=rec.topic_id, operator_id=999)

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-fast|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("✔")
        assert rec.interaction_state == "authorized"

    async def test_late_tap_after_timeout_does_not_authorize(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r5-B1: register -> await no_answer (retired), then a valid tap:
        claim returns "stale", advance_interaction_state is never reached,
        state stays awaiting_operator."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                   rid="ask-stale", topic_id=rec.topic_id, operator_id=999,
                   timeout_s=0.05)
        await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)  # times out

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-stale|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert rec.interaction_state == "awaiting_operator"

    async def test_late_tap_after_cancel_does_not_authorize(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """r5-B1: the cancel-flavoured sibling of the timeout case — a
        cancel_scope tombstone between register and tap also makes claim
        return "stale" without ever touching interaction_state."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        _seed(_fresh_broker, ns="engagement_ask", scope=rec.id, rid="ask-cxl2",
              topic_id=rec.topic_id, operator_id=999)
        _fresh_broker.cancel_scope(
            namespace="engagement_ask", scope=rec.id, reason="test_cancel",
        )

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-cxl2|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once_with("expired")
        assert rec.interaction_state == "awaiting_operator"

    async def test_cancel_during_blocked_persist_commits_authorized_ask(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """B4 (Sol diff r2): the callback task is cancelled while the tombstone
        write is BLOCKED (real registry, gated writer). advance shields its
        mutate+persist, so once the gate releases the durable write completes
        and the state is authorized. The callback must COMMIT the ask (the
        awaiting handler resolves ``answered``) rather than abort/re-arm it into
        ``no_answer``."""
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        registry = engagement_fixture.registry
        rec.interaction_state = "awaiting_operator"
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                    rid="ask-cxl-persist", topic_id=rec.topic_id,
                    operator_id=999)

        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_write = registry._write_tombstone_locked

        async def _gated_write(*, strict=False):
            entered.set()
            await gate.wait()
            await orig_write(strict=strict)

        registry._write_tombstone_locked = _gated_write

        update = _mk_callback_update(
            data="v1|engagement_ask|ask-cxl-persist|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        task = asyncio.create_task(ch._on_inline_callback(update, context=None))
        # Wait until the shielded write is in flight, THEN cancel the callback.
        await asyncio.wait_for(entered.wait(), 1.0)
        task.cancel()
        await asyncio.sleep(0)      # deliver the cancel at the shield await
        gate.set()                  # release the blocked durable write
        with pytest.raises(asyncio.CancelledError):
            await task

        # Durable authorization landed despite the cancel...
        assert rec.interaction_state == "authorized"
        # ...and the ask was COMMITTED, not re-armed into no_answer.
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 0.5)
        assert outcome["outcome"] == "answered"
        assert outcome["option_index"] == 0
        assert _fresh_broker.pending(
            namespace="engagement_ask", scope=rec.id,
        ) == []

    async def test_no_answer_leaves_awaiting_operator(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        """A timed-out ask (no_answer outcome) never touches
        interaction_state — no tap ever claimed it."""
        rec = engagement_fixture.active_record
        rec.interaction_state = "awaiting_operator"
        req = _seed(_fresh_broker, ns="engagement_ask", scope=rec.id,
                   rid="ask-noans", topic_id=rec.topic_id, operator_id=999,
                   timeout_s=0.05)
        outcome = await asyncio.wait_for(_fresh_broker.await_result(req), 1.0)
        assert outcome["outcome"] == "no_answer"
        assert rec.interaction_state == "awaiting_operator"


class TestKeyboardOwnership:
    async def test_callback_never_edits_the_keyboard(
        self, fake_telegram_bot, engagement_fixture, _fresh_broker,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        _seed(_fresh_broker, scope=rec.id, rid="rid-noedit",
              topic_id=rec.topic_id, operator_id=999)

        fake_telegram_bot.edit_message_reply_markup = AsyncMock()
        fake_telegram_bot.edit_message_text = AsyncMock()

        update = _mk_callback_update(
            data="v1|permission|rid-noedit|0", thread_id=rec.topic_id,
            chat_id=-1001, user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        fake_telegram_bot.edit_message_reply_markup.assert_not_awaited()
        fake_telegram_bot.edit_message_text.assert_not_awaited()

    async def test_composed_keyboard_callback_data_within_64_bytes(
        self, fake_telegram_bot, engagement_fixture,
    ):
        ch = _mk_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        rid = "f" * 32  # hooks._RID_MAX_LEN — the worst realistic case
        rec.topic_id = 1001

        captured: dict = {}

        async def _capture_send_to_topic(thread_id, text, **kwargs):
            captured["kwargs"] = kwargs
            return 1

        ch.send_to_topic = _capture_send_to_topic  # type: ignore[method-assign]

        mid = await ch.post_perm_keyboard(
            engagement_id=rec.id, request_id=rid, tool_name="Bash",
            tool_input={"command": "ls"},
        )
        assert mid == 1
        kbd = captured["kwargs"]["reply_markup"]
        for row in kbd.inline_keyboard:
            for btn in row:
                assert btn.callback_data.startswith("v1|permission|")
                assert len(btn.callback_data.encode("utf-8")) <= 64


class TestUpdateTopicState:
    """E-12 (v0.37.0) Task 23: TelegramChannel.update_topic_state."""

    async def test_update_state_edits_topic_title(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        # The engagement_fixture creates only a registry record with
        # topic_id=555. Pre-create the forum topic on the fake bot so
        # update_topic_state's edit_forum_topic finds it.
        # v0.37.1 D-1: title format is now "<state> <task>" (role is on
        # the bubble, not in the title).
        await fake_telegram_bot.create_forum_topic(
            chat_id=-1001, name="🟢 t",
        )
        # The fake's auto-assigned thread id starts at 1001; manually align
        # the fixture's topic_id to that value.
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )

        sg = fake_telegram_bot._supergroups[-1001]
        topic = sg.topics[1001]
        assert topic.name.startswith("🟡"), f"got {topic.name!r}"

    async def test_update_state_is_noop_when_state_unchanged(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        # Seed the registry with current_state_emoji=🟡 already.
        await engagement_fixture.registry.set_channel_state(
            rec.id, current_state_emoji="🟡",
        )

        # Sanity: edit_forum_topic shouldn't be called.
        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )
        ef.assert_not_awaited()

    async def test_update_state_unknown_state_drops_silently(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record

        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="not-a-real-state",
        )
        ef.assert_not_awaited()

    async def test_update_state_unknown_engagement_drops_silently(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry

        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id="does-not-exist", new_state="awaiting",
        )
        ef.assert_not_awaited()

    async def test_update_state_persists_current_state_emoji(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        await fake_telegram_bot.create_forum_topic(
            chat_id=-1001, name="🟢·🤖 t",
        )
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        assert rec.current_state_emoji is None  # baseline

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )
        assert rec.current_state_emoji == "🟡"
