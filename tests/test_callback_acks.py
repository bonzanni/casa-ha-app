"""``callback_acks.py`` — the durable, fail-closed consent store for
plugin-declared authorization callbacks (spec §4, INV-CB-003).

Structural sibling of ``trigger_acks.py``: same locking, atomic
persist-then-publish, and whole-store fail-closed load, but records are
keyed by :func:`plugin_callbacks.ack_identity` over ``(plugin, effective,
declaration_digest)`` — no artifact id, so a routine plugin upgrade that
leaves the declaration unchanged keeps its ack.
"""
import json

from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity


def test_record_get_roundtrip(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    rec = store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    got = store.get(identity)

    assert got is not None
    assert got["plugin"] == "elevenlabs"
    assert got["effective"] == "plg-elevenlabs--oauth"
    assert got["declaration_digest"] == "digest-1"
    assert isinstance(got["gen"], str) and got["gen"]
    assert isinstance(got["ts"], int)
    assert got == rec


def test_get_missing_identity_returns_none(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    assert store.get("nonexistent") is None


def test_restart_load_sees_prior_ack(tmp_path):
    path = tmp_path / "acks.json"
    store1 = CallbackAckStore(path=path)
    store1.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    store2 = CallbackAckStore(path=path)
    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    got = store2.get(identity)

    assert got is not None
    assert got["plugin"] == "elevenlabs"


def test_record_idempotent_keeps_generation(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    first = store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    second = store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    assert first["gen"] == second["gen"]


def test_corrupt_json_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = CallbackAckStore(path=path)

    assert store.get(ack_identity("elevenlabs", "eff", "digest-1")) is None


def test_wrong_schema_version_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    path.write_text(json.dumps({
        "schema_version": 999,
        "acks": {
            identity: {
                "plugin": "elevenlabs", "effective": "eff",
                "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
            },
        },
    }), encoding="utf-8")

    store = CallbackAckStore(path=path)

    assert store.get(identity) is None


def test_identity_key_mismatch_yields_zero_acks_whole_store(tmp_path):
    """INV-CB-003 red case: a record whose recomputed identity != its key
    must never load — and it takes down the WHOLE store, not just the one
    bad entry, since a hand-edited or merged file can no longer be trusted
    at all."""
    path = tmp_path / "acks.json"
    good_identity = ack_identity("elevenlabs", "eff-good", "digest-1")
    bad_key = "not-the-real-identity"
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {
            good_identity: {
                "plugin": "elevenlabs", "effective": "eff-good",
                "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
            },
            bad_key: {
                "plugin": "other", "effective": "eff-bad",
                "declaration_digest": "digest-2", "ts": 2, "gen": "g2",
            },
        },
    }), encoding="utf-8")

    store = CallbackAckStore(path=path)

    # Even the record whose key WAS correct fails to load: whole-store
    # fail-closed, not per-record filtering.
    assert store.get(good_identity) is None


def test_malformed_record_missing_field_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {
            identity: {
                "plugin": "elevenlabs", "effective": "eff",
                # declaration_digest missing entirely
                "ts": 1, "gen": "g1",
            },
        },
    }), encoding="utf-8")

    store = CallbackAckStore(path=path)

    assert store.get(identity) is None


def test_revoke_plugin_returns_removed_and_persists(tmp_path):
    path = tmp_path / "acks.json"
    store = CallbackAckStore(path=path)
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    store.record("elevenlabs", "plg-elevenlabs--callback2", "digest-2")
    store.record("other-plugin", "plg-other-plugin--oauth", "digest-3")

    removed = store.revoke_plugin("elevenlabs")

    assert len(removed) == 2
    assert {r["plugin"] for r in removed} == {"elevenlabs"}

    other_identity = ack_identity("other-plugin", "plg-other-plugin--oauth", "digest-3")
    ev_identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    assert store.get(ev_identity) is None
    assert store.get(other_identity) is not None

    # Persisted: a fresh store instance over the same file agrees.
    reloaded = CallbackAckStore(path=path)
    assert reloaded.get(ev_identity) is None
    assert reloaded.get(other_identity) is not None


def test_revoke_plugin_no_match_returns_empty_and_does_not_persist(tmp_path):
    path = tmp_path / "acks.json"
    store = CallbackAckStore(path=path)
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    before = path.read_text(encoding="utf-8")

    removed = store.revoke_plugin("nonexistent")

    assert removed == []
    assert path.read_text(encoding="utf-8") == before


def test_prune_stale_drops_only_identities_outside_valid_set(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    store.record("elevenlabs", "plg-elevenlabs--callback2", "digest-2")
    store.record("other-plugin", "plg-other-plugin--oauth", "digest-3")

    keep_identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    stale_identity = ack_identity("elevenlabs", "plg-elevenlabs--callback2", "digest-2")
    other_identity = ack_identity("other-plugin", "plg-other-plugin--oauth", "digest-3")

    removed = store.prune_stale({keep_identity, other_identity})

    assert len(removed) == 1
    assert removed[0]["declaration_digest"] == "digest-2"
    assert store.get(stale_identity) is None
    assert store.get(keep_identity) is not None
    assert store.get(other_identity) is not None
