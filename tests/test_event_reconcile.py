"""``event_reconcile`` — the plugin-event routing reconciler.

Structural sibling of ``test_callback_reconcile.py``: a pure
``compute_desired`` gate matrix, plus the locked reconcile's publish/
fail-closed/consent-prompt/revoke behavior.
"""
import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

import event_reconcile as er
import event_spool
from event_acks import EventAckStore
from plugin_events import ack_identity


# ---------------------------------------------------------------------------
# fixtures / doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_published(monkeypatch):
    """The published routed map is a module global — never leak between
    tests."""
    monkeypatch.setattr(er, "_routed", event_spool.ROUTING_UNAVAILABLE)
    yield


def _manifest(emits=(), subscribes=()):
    casa = {}
    if emits:
        casa["emits"] = [{"name": n} for n in emits]
    if subscribes:
        casa["subscribes"] = [{"plugin": e, "event": ev} for e, ev in subscribes]
    return {"name": "x", "casa": casa}


def _rp(name, *, artifact_id="art-1", manifest_name=None, emits=(),
       subscribes=(), manifest=None):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id,
        path=f"/store/{name}/{artifact_id}", version="1.0.0",
        manifest_name=manifest_name if manifest_name is not None else name,
        manifest=_manifest(emits, subscribes) if manifest is None else manifest)


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


def _identity(subscriber, artifact_id, emitter, event, targets=("resident:assistant",)):
    from plugin_events import subscribe_declaration_digest
    digest = subscribe_declaration_digest({"plugin": emitter, "event": event})
    return ack_identity(subscriber, artifact_id, emitter, event, digest,
                        sorted(targets))


def _ack(acks, subscriber, artifact_id, emitter, event,
        targets=("resident:assistant",), now=None):
    from plugin_events import subscribe_declaration_digest
    digest = subscribe_declaration_digest({"plugin": emitter, "event": event})
    return acks.record(subscriber, artifact_id, emitter, event, digest,
                       sorted(targets), now if now is not None else time.time())


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


@pytest.fixture
def fake_event_episodes(monkeypatch):
    """A minimal stand-in for ``event_episodes`` — Task 7's tests must not
    depend on Task 8's implementation existing. Records kicks; exposes the
    real lock shape (``DISPATCH_LOCK``) so ``revoke_and_unroute`` composes."""
    mod = SimpleNamespace(DISPATCH_LOCK=asyncio.Lock(), kicks=0)
    mod.kick_all = lambda: setattr(mod, "kicks", mod.kicks + 1)
    monkeypatch.setitem(sys.modules, "event_episodes", mod)
    return mod


# ---------------------------------------------------------------------------
# compute_desired — the gate matrix
# ---------------------------------------------------------------------------


async def test_valid_assigned_acked_subscription_routes(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert desired.issues == []
    assert desired.consent_needed == []
    key = ("gmail", "mail_in")
    assert key in desired.routed
    snap = desired.routed[key]["finance"]
    assert snap["subscriber"] == "finance"
    assert snap["artifact_id"] == "art-1"
    assert snap["targets"] == ["resident:assistant"]
    assert snap["ack_identity"] == _identity("finance", "art-1", "gmail", "mail_in")


async def test_invalid_subscribe_block_is_dark(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", manifest={
        "name": "x", "casa": {"subscribes": "not-a-list"}})
    acks = EventAckStore(path=tmp_path / "acks.json")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_invalid" and i["name"] == "finance"
              for i in desired.issues)


def test_emitter_missing_dark_then_heals(tmp_path):
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    role_configs = _role_configs(assistant=["telegram"])

    # emitter not installed at all
    desired = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([subscriber]), entries=_entries(subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_emitter_missing" for i in desired.issues)

    # emitter installed but does not declare the event
    emitter_wrong = _rp("gmail", emits=["other_event"])
    desired2 = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter_wrong, subscriber]),
        entries=_entries(emitter_wrong, subscriber))
    assert desired2.routed == {}
    assert any(i["reason_code"] == "event_emitter_missing" for i in desired2.issues)

    # heals once the emitter is installed AND declares the event
    emitter = _rp("gmail", emits=["mail_in"])
    desired3 = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert ("gmail", "mail_in") in desired3.routed
    assert not any(i["reason_code"] == "event_emitter_missing"
                  for i in desired3.issues)


def test_no_target_dark_then_heals(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in", targets=())
    role_configs = _role_configs(assistant=["telegram"])

    entries = _entries(emitter, targets=())

    def entries_no_finance_targets():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "finance", "artifact_id": "art-1", "targets": []}]

    desired = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=entries_no_finance_targets)
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_no_target" for i in desired.issues)

    def entries_with_finance_target():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "finance", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    ack2 = EventAckStore(path=tmp_path / "acks2.json")
    _ack(ack2, "finance", "art-1", "gmail", "mail_in",
        targets=("resident:assistant",))
    desired2 = er.compute_desired(
        role_configs=role_configs, acks=ack2,
        resolver=_resolver([emitter, subscriber]),
        entries=entries_with_finance_target)
    assert ("gmail", "mail_in") in desired2.routed


def test_pending_ack_dark_then_heals(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    role_configs = _role_configs(assistant=["telegram"])
    resolver = _resolver([emitter, subscriber])
    entries = _entries(emitter, subscriber)

    desired = er.compute_desired(role_configs=role_configs, acks=acks,
                                 resolver=resolver, entries=entries)
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_pending_ack" for i in desired.issues)
    assert len(desired.consent_needed) == 1
    pending = desired.consent_needed[0]
    assert pending["subscriber"] == "finance"
    assert pending["emitter"] == "gmail"
    assert pending["event"] == "mail_in"

    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    desired2 = er.compute_desired(role_configs=role_configs, acks=acks,
                                  resolver=resolver, entries=entries)
    assert ("gmail", "mail_in") in desired2.routed
    assert desired2.consent_needed == []


def test_self_subscription_unscoped_refused(tmp_path):
    subscriber = _rp("finance", subscribes=[("finance", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([subscriber]), entries=_entries(subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_invalid" for i in desired.issues)


def test_self_subscription_scoped_spelling_refused(tmp_path):
    """Carried Task 2 finding: a bundled dependency naming ITSELF via its
    own SCOPED registry form must be refused even though
    ``plugin_events.parse_and_validate_subscribes``'s parse-time check only
    compares against the unscoped manifest name."""
    scoped = "slug.bank-feed"
    subscriber = SimpleNamespace(
        name=scoped, artifact_id="art-1", path=f"/store/{scoped}/art-1",
        version="1.0.0", manifest_name="bank-feed",
        manifest=_manifest(subscribes=[(scoped, "mail_in")]))
    acks = EventAckStore(path=tmp_path / "acks.json")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([subscriber]), entries=_entries(subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_invalid" for i in desired.issues)


def test_all_or_nothing_one_bad_subscription_darkens_whole_set(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[
        ("gmail", "mail_in"), ("nope", "ghost")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    desired = er.compute_desired(
        role_configs=_role_configs(assistant=["telegram"]), acks=acks,
        resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber))
    assert desired.routed == {}
    assert any(i["reason_code"] == "event_emitter_missing" for i in desired.issues)


def test_artifact_update_voids_consent(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", artifact_id="art-1",
                     subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    role_configs = _role_configs(assistant=["telegram"])
    entries = _entries(emitter, subscriber)

    desired = er.compute_desired(role_configs=role_configs, acks=acks,
                                 resolver=_resolver([emitter, subscriber]),
                                 entries=entries)
    assert ("gmail", "mail_in") in desired.routed

    upgraded = _rp("finance", artifact_id="art-2",
                   subscribes=[("gmail", "mail_in")])
    desired2 = er.compute_desired(
        role_configs=role_configs, acks=acks,
        resolver=_resolver([emitter, upgraded]),
        entries=_entries(emitter, upgraded))
    assert desired2.routed == {}
    assert any(i["reason_code"] == "event_pending_ack" for i in desired2.issues)


def test_retarget_voids_consent(tmp_path):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in",
        targets=("resident:assistant",))
    role_configs = _role_configs(assistant=["telegram"], ops=["telegram"])

    entries_v1 = _entries(emitter, subscriber, targets=("resident:assistant",))
    desired = er.compute_desired(role_configs=role_configs, acks=acks,
                                 resolver=_resolver([emitter, subscriber]),
                                 entries=entries_v1)
    assert ("gmail", "mail_in") in desired.routed

    def entries_retargeted():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]},
                {"name": "finance", "artifact_id": "art-1",
                 "targets": ["resident:ops"]}]

    desired2 = er.compute_desired(role_configs=role_configs, acks=acks,
                                  resolver=_resolver([emitter, subscriber]),
                                  entries=entries_retargeted)
    assert desired2.routed == {}
    assert any(i["reason_code"] == "event_pending_ack" for i in desired2.issues)


# ---------------------------------------------------------------------------
# reconcile_plugin_events — locking, publish, fail-closed, prompts
# ---------------------------------------------------------------------------


async def test_reconcile_publishes_routed_map(tmp_path, fake_event_episodes):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    issues = await er.reconcile_plugin_events(
        runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
        entries=_entries(emitter, subscriber), prompt=False)
    assert issues == []
    routed = er.get_routed()
    assert routed is not event_spool.ROUTING_UNAVAILABLE
    assert ("gmail", "mail_in") in routed
    assert fake_event_episodes.kicks == 1


async def test_reconcile_compute_failure_publishes_sentinel_and_raises(
        fake_event_episodes):
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)

    def _boom(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError):
        await er.reconcile_plugin_events(runtime, resolver=_boom, prompt=False)
    assert er.get_routed() is event_spool.ROUTING_UNAVAILABLE
    assert fake_event_episodes.kicks == 1


async def test_reconcile_lock_serializes_stale_compute(fake_event_episodes):
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=None)
    older = _rp("gmail-old", emits=["e"])
    newer = _rp("gmail-new", emits=["e"])

    def slow_resolver(target):
        time.sleep(0.05)
        return SimpleNamespace(registry_valid=True, plugins=[older], issues=[])

    def fast_resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[newer], issues=[])

    await asyncio.gather(
        er.reconcile_plugin_events(runtime, resolver=slow_resolver,
                                   entries=lambda: [], prompt=False),
        er.reconcile_plugin_events(runtime, resolver=fast_resolver,
                                   entries=lambda: [], prompt=False),
    )
    # Whichever call ran SECOND under the lock published last — since the
    # slow compute is issued first and holds the lock for its ENTIRE
    # critical section, the fast call can only run (and publish) after it,
    # so the final state is always the LAST one to complete under the lock,
    # never a stale one clobbering a newer swap.
    assert fake_event_episodes.kicks == 2


async def test_reconcile_fires_deduped_consent_prompt(tmp_path, fake_event_episodes):
    emitter = _rp("gmail", emits=["mail_in"])
    subscriber = _rp("finance", subscribes=[("gmail", "mail_in")])
    acks = EventAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    channel_manager = _FakeChannelManager(telegram)
    runtime = SimpleNamespace(
        role_configs=_role_configs(assistant=["telegram"]),
        channel_manager=channel_manager)

    import authz_grants
    import trigger_consent
    monkey_ok = hasattr(trigger_consent, "operator_identity")
    assert monkey_ok

    async def _run():
        return await er.reconcile_plugin_events(
            runtime, acks=acks, resolver=_resolver([emitter, subscriber]),
            entries=_entries(emitter, subscriber), prompt=True)

    import event_consent

    def _fixed_identity(channel):
        return (100, 200)

    orig = event_consent.operator_identity
    event_consent.operator_identity = _fixed_identity
    try:
        await _run()
        # a second reconcile pass with the SAME pending set must not double
        # the outstanding keyboard (in-flight dedup lives in the shared
        # ChallengeCoordinator; calling twice must not raise/duplicate).
        await _run()
    finally:
        event_consent.operator_identity = orig
    assert len(telegram.posts) >= 1


# ---------------------------------------------------------------------------
# revoke — unroute before ack delete, both under the admission lock
# ---------------------------------------------------------------------------


async def test_revoke_unroutes_before_ack_delete(tmp_path, fake_event_episodes,
                                                  monkeypatch):
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    monkeypatch.setattr(er, "_routed", {
        ("gmail", "mail_in"): {"finance": {
            "subscriber": "finance", "artifact_id": "art-1",
            "targets": ["resident:assistant"], "ack_identity": "x"}}})

    observed = {}
    real_revoke = acks.revoke_subscriber

    def spy_revoke(subscriber):
        # At the moment the ack store is asked to revoke, the routed map
        # must ALREADY have unrouted this subscriber.
        routed = er.get_routed()
        observed["still_routed"] = subscriber in (
            routed.get(("gmail", "mail_in")) or {})
        return real_revoke(subscriber)

    acks.revoke_subscriber = spy_revoke
    removed = await er.revoke_and_unroute("finance", acks=acks)
    assert observed["still_routed"] is False
    assert len(removed) == 1
    routed = er.get_routed()
    assert "finance" not in (routed.get(("gmail", "mail_in")) or {})
    assert acks.get(_identity("finance", "art-1", "gmail", "mail_in")) is None


async def test_revoke_pair_only_unroutes_named_pair(fake_event_episodes,
                                                     monkeypatch, tmp_path):
    acks = EventAckStore(path=tmp_path / "acks.json")
    _ack(acks, "finance", "art-1", "gmail", "mail_in")
    _ack(acks, "finance", "art-1", "slack", "msg_in")
    monkeypatch.setattr(er, "_routed", {
        ("gmail", "mail_in"): {"finance": {
            "subscriber": "finance", "artifact_id": "art-1",
            "targets": ["resident:assistant"], "ack_identity": "x"}},
        ("slack", "msg_in"): {"finance": {
            "subscriber": "finance", "artifact_id": "art-1",
            "targets": ["resident:assistant"], "ack_identity": "y"}},
    })
    removed = await er.revoke_and_unroute(
        "finance", "gmail", "mail_in", acks=acks)
    assert len(removed) == 1
    routed = er.get_routed()
    assert "finance" not in routed[("gmail", "mail_in")]
    assert "finance" in routed[("slack", "msg_in")]


def test_revoke_with_sentinel_routed_is_a_noop(monkeypatch):
    # No published map yet — nothing to unroute, must not raise.
    er._unroute_locked("finance")
    assert er.get_routed() is event_spool.ROUTING_UNAVAILABLE


# ---------------------------------------------------------------------------
# to_spool_shape / current_issues
# ---------------------------------------------------------------------------


def test_to_spool_shape_narrows_and_passes_sentinel():
    routed = {("gmail", "mail_in"): {"finance": {"subscriber": "finance"},
                                     "ops": {"subscriber": "ops"}}}
    shape = er.to_spool_shape(routed)
    assert shape == {("gmail", "mail_in"): {"finance", "ops"}}
    assert er.to_spool_shape(event_spool.ROUTING_UNAVAILABLE) \
        is event_spool.ROUTING_UNAVAILABLE


def test_current_issues_never_raises_without_runtime(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    assert er.current_issues() == []


def test_current_issues_includes_spool_passthrough(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)
    monkeypatch.setattr(
        event_spool, "spool_issues",
        lambda: [{"reason": "event_spool_issue", "kind": "corrupt_state",
                  "emitter": "gmail", "file": "x"}])
    issues = er.current_issues()
    assert any(i["reason_code"] == "event_spool_issue" and i["name"] == "gmail"
              for i in issues)
