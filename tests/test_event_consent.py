"""The event-consent DM round.

A plugin's ``casa.subscribes`` entry delivers events only after the operator
taps Approve on a DM keyboard bound to the subscription's full consent
identity (subscriber + subscriber artifact id + emitter + event + digest +
sorted targets). Structural sibling of ``callback_consent`` (same ack
persistence discipline, same setup-round feed) — mirrored here.
"""
import asyncio

import pytest

import event_consent as ec
from authz_grants import ChallengeCoordinator
from event_acks import EventAckStore
from plugin_events import ack_identity, subscribe_declaration_digest

SUBSCRIBER = "finance"
ARTIFACT_ID = "art-1"
EMITTER = "gmail"
EVENT = "new-mail"
DIGEST = subscribe_declaration_digest({"plugin": EMITTER, "event": EVENT})
TARGETS = ["resident:assistant"]
IDENTITY = ack_identity(SUBSCRIBER, ARTIFACT_ID, EMITTER, EVENT, DIGEST, TARGETS)


class _FakeChannel:
    def __init__(self) -> None:
        self.posts: list = []
        self.edits: list = []
        self.dispatches: list = []
        self.chat_id = "100"

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_button_continuation(self, **kw):
        self.dispatches.append(kw)
        return True


def _fresh_env(monkeypatch, tmp_path):
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = ChallengeCoordinator()
    channel = _FakeChannel()
    acks = EventAckStore(path=tmp_path / "event_acks.json")
    return broker, coord, channel, acks


def _prompt(coord, channel, acks, *, reconcile_cb=None, **over):
    kw = dict(coordinator=coord, channel=channel, chat_id=100, operator_id=100,
              subscriber=SUBSCRIBER, artifact_id=ARTIFACT_ID, emitter=EMITTER,
              event=EVENT, digest=DIGEST, targets=TARGETS, acks=acks,
              reconcile_cb=reconcile_cb)
    kw.update(over)
    return ec.prompt_event_consent(**kw)


async def _settle(n: int = 8):
    for _ in range(n):
        await asyncio.sleep(0)


def _tap(broker, coord, key, idx, *, actor=100):
    ch = coord._entries[key]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=idx, actor_id=actor)
    assert not isinstance(claim, str), f"claim rejected: {claim}"
    assert broker.commit(claim) is True
    step = ch.req.meta.get("on_commit_sync")
    if step is not None:
        step(idx)
    return ch


def _wire_episodes(monkeypatch, tmp_path):
    import plugin_setup_episodes as pse
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)

    async def _dispatch(role, instruction, ctx):
        return True

    async def _notify(text):
        return None

    pse.configure(
        dispatch=_dispatch, notify_operator=_notify,
        resolve_registry_entry=lambda p: None,
        ack_lookup=lambda ident: None, routes_live=lambda p: True)
    return pse


# ---------------------------------------------------------------------------
# TTL / constant pins
# ---------------------------------------------------------------------------


def test_ttl_value():
    assert ec.EVENT_CONSENT_TTL_S == 600.0


# ---------------------------------------------------------------------------
# the prompt / render
# ---------------------------------------------------------------------------


def test_message_names_subscriber_emitter_event_and_target_roles():
    text = ec.render_event_consent_message(
        subscriber=SUBSCRIBER, emitter=EMITTER, event=EVENT, targets=TARGETS)
    assert SUBSCRIBER in text
    assert EMITTER in text
    assert EVENT in text
    assert "assistant" in text


def test_message_names_multiple_target_roles():
    text = ec.render_event_consent_message(
        subscriber=SUBSCRIBER, emitter=EMITTER, event=EVENT,
        targets=["resident:assistant", "specialist:analyst"])
    assert "assistant" in text
    assert "analyst" in text


async def test_prompt_posts_the_keyboard(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    assert handle.created is True
    await handle.settled_post()
    chat_id, _rid, text, options = channel.posts[0]
    assert chat_id == 100
    assert options == ("Approve", "Deny")
    assert SUBSCRIBER in text
    assert EMITTER in text
    assert EVENT in text


async def test_duplicate_prompt_is_deduped(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    h1 = _prompt(coord, channel, acks)
    h2 = _prompt(coord, channel, acks)
    assert h1.created is True
    assert h2.created is False
    await h1.settled_post()
    assert len(channel.posts) == 1


async def test_operator_identity_is_the_trigger_consent_rule(monkeypatch,
                                                             tmp_path):
    ch = _FakeChannel()
    ch.chat_id = "-100123"       # a group chat: nobody is the operator
    assert ec.operator_identity(ch) is None
    ch.chat_id = "4242"
    assert ec.operator_identity(ch) == (4242, 4242)


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


async def test_approve_records_the_ack_synchronously(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    _wire_episodes(monkeypatch, tmp_path)
    order: list = []

    async def _reconcile():
        order.append("reconcile")

    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    key = next(iter(coord._entries))
    _tap(broker, coord, key, 0)
    # persisted BEFORE any await — the commit step is the durability point
    assert acks.get(IDENTITY) is not None
    await _settle()
    assert order == ["reconcile"]
    assert any("✅" in e[2] for e in channel.edits)
    assert channel.dispatches == []          # never an agent continuation


async def test_ack_survives_a_crash_right_after_the_commit_step(
    monkeypatch, tmp_path,
):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    _wire_episodes(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    ch = coord._entries[next(iter(coord._entries))]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=100)
    assert broker.commit(claim) is True
    ch.req.meta["on_commit_sync"](0)
    # nothing else runs — reload the store from disk
    reloaded = EventAckStore(path=tmp_path / "event_acks.json")
    assert reloaded.get(IDENTITY) is not None


async def test_deny_leaves_it_undelivered(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    _wire_episodes(monkeypatch, tmp_path)
    fired: list = []

    async def _reconcile():
        fired.append(True)

    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    key = next(iter(coord._entries))
    _tap(broker, coord, key, 1)
    await _settle()
    assert acks.get(IDENTITY) is None
    assert fired == []
    assert any("❌" in e[2] for e in channel.edits)


async def test_expiry_leaves_it_undelivered(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    _wire_episodes(monkeypatch, tmp_path)
    monkeypatch.setattr(ec, "EVENT_CONSENT_TTL_S", 0.02)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    await asyncio.sleep(0.1)
    await _settle()
    assert acks.get(IDENTITY) is None
    assert any("⌛" in e[2] for e in channel.edits)


async def test_failed_ack_write_never_starts_delivery(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    _wire_episodes(monkeypatch, tmp_path)
    fired: list = []

    async def _reconcile():
        fired.append(True)

    def _boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(acks, "record", _boom)
    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    ch = coord._entries[next(iter(coord._entries))]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=100)
    assert broker.commit(claim) is True
    with pytest.raises(RuntimeError):
        ch.req.meta["on_commit_sync"](0)      # the telegram handler swallows
    await _settle()
    assert fired == []
    assert any("internal error" in e[2] for e in channel.edits)


async def test_reconcile_failure_warns_but_keeps_the_ack(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    _wire_episodes(monkeypatch, tmp_path)

    async def _boom():
        raise RuntimeError("reconcile failed")

    handle = _prompt(coord, channel, acks, reconcile_cb=_boom)
    await handle.settled_post()
    _tap(broker, coord, next(iter(coord._entries)), 0)
    await _settle()
    assert acks.get(IDENTITY) is not None
    assert "⚠️" in channel.edits[-1][2]


async def test_revoke_cancels_a_pending_keyboard(monkeypatch, tmp_path):
    """The revoke path kills a subscriber's live consent keyboards
    (``cancel_matching(plugin=…)``, the generic filter every consent-kind
    key exposes via a field literally named ``plugin``), so a stale Approve
    tap can never undo it."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    assert coord.cancel_matching(plugin=SUBSCRIBER) == 1
    await _settle()
    assert acks.get(IDENTITY) is None


async def test_lifecycle_cancel_by_artifact_kills_the_keyboard(monkeypatch,
                                                               tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    assert coord.cancel_matching(artifact=ARTIFACT_ID) == 1
    await _settle()
    assert acks.get(IDENTITY) is None


async def test_callback_approval_is_recorded_in_the_round(monkeypatch,
                                                          tmp_path):
    """The approval must land in the setup ledger under the SUBSCRIBER as
    ``plugin``, inside the same yield-free commit step that persists the
    ack — the same ledger trigger/callback consent share."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    pse = _wire_episodes(monkeypatch, tmp_path)
    pse.open_round(plugin=SUBSCRIBER, artifact_id=ARTIFACT_ID,
                  identities=[IDENTITY])
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    _tap(broker, coord, next(iter(coord._entries)), 0)
    member = pse._load()["rounds"][SUBSCRIBER]["members"][IDENTITY]
    assert member["state"] == "approved"
    assert member["gen"] == acks.get(IDENTITY)["gen"]
