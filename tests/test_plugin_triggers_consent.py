"""Release B — consent ack identity + the persistent trigger-ack store.

The ack IDENTITY binds an operator's consent to exactly
(plugin, artifact_id, effective, target, normalized-auth-policy): any change
to any component is a NEW identity that requires a fresh consent tap.

The STORE (`trigger_acks.TriggerAckStore`) persists structured ack records
(not bare hashes) so lifecycle revocation can find what to unroute and which
per-trigger secrets to retire. It is atomic (crash mid-write never corrupts
consent state into an open route) and fail-closed (an unreadable/corrupt
store means NO acks).
"""
import json

import pytest

from plugin_triggers import ack_identity
from trigger_acks import TriggerAckStore

AUTH = {"mode": "static_header", "header": "X-API-Key",
        "tolerance_secs": 300, "secret_owner": "casa"}


def _identity(**over):
    kw = dict(plugin="elevenlabs", artifact_id="art-1",
              effective="plg-elevenlabs--voicemail",
              target="resident:assistant", auth=AUTH)
    kw.update(over)
    return ack_identity(**kw)


# ---------------------------------------------------------------------------
# ack_identity
# ---------------------------------------------------------------------------


def test_identity_is_stable_for_equal_inputs():
    assert _identity() == _identity()


def test_identity_ignores_auth_key_order():
    reordered = {"secret_owner": "casa", "tolerance_secs": 300,
                 "header": "X-API-Key", "mode": "static_header"}
    assert _identity() == _identity(auth=reordered)


@pytest.mark.parametrize("over", [
    {"plugin": "other"},
    {"artifact_id": "art-2"},
    {"effective": "plg-elevenlabs--other"},
    {"target": "resident:butler"},
    {"auth": {**AUTH, "mode": "timestamped_hmac"}},
    {"auth": {**AUTH, "header": "X-Other"}},
    {"auth": {**AUTH, "tolerance_secs": 600}},
])
def test_identity_changes_when_any_component_changes(over):
    assert _identity(**over) != _identity()


def test_identity_is_a_hex_digest():
    ident = _identity()
    assert len(ident) == 64
    int(ident, 16)  # parses as hex


# ---------------------------------------------------------------------------
# TriggerAckStore
# ---------------------------------------------------------------------------


def _record(store, **over):
    kw = dict(identity=_identity(**over) if over else _identity(),
              plugin="elevenlabs", artifact_id="art-1",
              effective="plg-elevenlabs--voicemail",
              target="resident:assistant", auth=AUTH)
    for k in ("plugin", "artifact_id", "effective", "target", "auth"):
        if k in over:
            kw[k] = over[k]
    store.record(**kw)
    return kw["identity"]


def test_record_then_is_acked(tmp_path):
    store = TriggerAckStore(path=tmp_path / "acks.json")
    ident = _record(store)
    assert store.is_acked(ident)
    assert not store.is_acked("0" * 64)


def test_persists_across_reload(tmp_path):
    path = tmp_path / "acks.json"
    ident = _record(TriggerAckStore(path=path))
    assert TriggerAckStore(path=path).is_acked(ident)


def test_revoke_plugin_removes_and_returns_structured_records(tmp_path):
    store = TriggerAckStore(path=tmp_path / "acks.json")
    ident = _record(store)
    other = _record(store, plugin="other", artifact_id="art-9",
                    effective="plg-other--hook")
    removed = store.revoke_plugin("elevenlabs")
    assert [r["effective"] for r in removed] == ["plg-elevenlabs--voicemail"]
    assert not store.is_acked(ident)
    assert store.is_acked(other)  # other plugins untouched
    # revocation persists
    assert not TriggerAckStore(path=store.path).is_acked(ident)


def test_revoke_artifact_removes_only_that_artifact(tmp_path):
    store = TriggerAckStore(path=tmp_path / "acks.json")
    old = _record(store)
    new = _record(store, artifact_id="art-2")
    removed = store.revoke_artifact("art-1")
    assert [r["artifact_id"] for r in removed] == ["art-1"]
    assert not store.is_acked(old)
    assert store.is_acked(new)


def test_revoke_missing_is_noop(tmp_path):
    store = TriggerAckStore(path=tmp_path / "acks.json")
    assert store.revoke_plugin("ghost") == []
    assert store.revoke_artifact("ghost") == []


def test_record_is_idempotent(tmp_path):
    store = TriggerAckStore(path=tmp_path / "acks.json")
    ident = _record(store)
    _record(store)
    assert store.is_acked(ident)
    assert len(store.revoke_plugin("elevenlabs")) == 1


def test_corrupt_store_fails_closed(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text("{not json", encoding="utf-8")
    store = TriggerAckStore(path=path)
    assert not store.is_acked(_identity())
    # and recovers on the next record (rewrites a valid store)
    ident = _record(store)
    assert TriggerAckStore(path=path).is_acked(ident)


def test_missing_store_means_no_acks(tmp_path):
    store = TriggerAckStore(path=tmp_path / "nope" / "acks.json")
    assert not store.is_acked(_identity())


def test_wrong_shape_store_fails_closed(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text(json.dumps({"acks": ["not", "a", "dict"]}),
                    encoding="utf-8")
    assert not TriggerAckStore(path=path).is_acked(_identity())


def test_stored_records_carry_structured_metadata(tmp_path):
    path = tmp_path / "acks.json"
    store = TriggerAckStore(path=path)
    ident = _record(store)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    rec = on_disk["acks"][ident]
    assert rec["plugin"] == "elevenlabs"
    assert rec["artifact_id"] == "art-1"
    assert rec["effective"] == "plg-elevenlabs--voicemail"
    assert rec["target"] == "resident:assistant"
    assert rec["auth"]["mode"] == "static_header"
