"""``callback_acks.py`` — the durable, fail-closed consent store for
plugin-declared authorization callbacks (INV-CB-003).

Structural sibling of ``trigger_acks.py``: same locking, atomic
persist-then-publish, and whole-store fail-closed load, but records are
keyed by :func:`plugin_callbacks.ack_identity` over ``(plugin, effective,
declaration_digest)`` — no artifact id, so a routine plugin upgrade that
leaves the declaration unchanged keeps its ack.
"""
import json

import pytest

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


def _store_with_record(path, rec):
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {identity: rec},
    }), encoding="utf-8")
    return identity


def test_non_numeric_ts_yields_zero_acks_whole_store(tmp_path):
    """INV-CB-003: ``ts`` must be a real finite number. A string ``ts`` is a
    malformed record and fails the WHOLE store, same as a bad identity."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": "not-a-number", "gen": "g1",
    })
    assert CallbackAckStore(path=path).get(identity) is None


def test_bool_ts_yields_zero_acks_whole_store(tmp_path):
    """A bool is a subclass of int but is not a real timestamp — rejected."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": True, "gen": "g1",
    })
    assert CallbackAckStore(path=path).get(identity) is None


def test_extra_field_yields_zero_acks_whole_store(tmp_path):
    """An otherwise-valid record carrying a key outside the exact set
    {plugin, effective, declaration_digest, gen, ts} means the file was
    written by something other than this store — the whole store fails."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
        "unexpected": "surprise",
    })
    assert CallbackAckStore(path=path).get(identity) is None


def test_clean_record_still_loads(tmp_path):
    """The tightened schema must not reject a well-formed record."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
    })
    got = CallbackAckStore(path=path).get(identity)
    assert got is not None and got["gen"] == "g1"


def test_float_ts_is_accepted(tmp_path):
    """A float ``ts`` (a legitimate ``time.time()`` shape) still loads."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": 1.5, "gen": "g1",
    })
    assert CallbackAckStore(path=path).get(identity) is not None


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


def test_record_publishes_only_after_durable_write(tmp_path, monkeypatch):
    """A failed persist must raise AND leave the in-memory view unchanged —
    a reconcile racing the failure can never route an ack that would vanish
    on reboot (mirrors trigger_acks.py's identical pin)."""
    store = CallbackAckStore(path=tmp_path / "acks.json")

    def _boom(path, text):
        raise OSError("disk full")

    import atomic_io
    monkeypatch.setattr(atomic_io, "atomic_write_text", _boom)

    with pytest.raises(OSError):
        store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    assert store.get(identity) is None


def test_revoke_publishes_only_after_durable_write(tmp_path, monkeypatch):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    def _boom(path, text):
        raise OSError("disk full")

    import atomic_io
    monkeypatch.setattr(atomic_io, "atomic_write_text", _boom)

    with pytest.raises(OSError):
        store.revoke_plugin("elevenlabs")

    # memory unchanged: the revoke did NOT half-apply (a crash would have
    # silently resurrected it from disk otherwise)
    assert store.get(identity) is not None


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
