"""Crash-safe secret rotation state machine (Release A, Task 5, spec A2c)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from webhook_auth import (
    ensure_secret,
    read_secret,
    rotation_begin,
    rotation_import_next,
    rotation_promote,
    rotation_recover,
)


def _seed_live(name: str, tmp_path: Path, owner: str = "casa") -> bytes:
    val = ensure_secret(name, owner=owner, secrets_dir=tmp_path)
    assert val is not None
    return val


def test_casa_begin_mints_next_and_goes_staged(tmp_path: Path):
    _seed_live("vm", tmp_path)
    phase = rotation_begin("vm", owner="casa", secrets_dir=tmp_path)
    assert phase == "staged"
    assert (tmp_path / "vm.next").exists()
    state = json.loads((tmp_path / "vm.rot.json").read_text())
    assert state["phase"] == "staged"


def test_provider_begin_awaiting_then_import_staged(tmp_path: Path):
    # provider live secret imported out of band for the test
    (tmp_path / "vm").write_bytes(b"provider-live-secret-value")
    (tmp_path / "vm").chmod(0o600)
    phase = rotation_begin("vm", owner="provider", secrets_dir=tmp_path)
    assert phase == "awaiting_next"
    assert not (tmp_path / "vm.next").exists()  # no Casa-minted next

    phase = rotation_import_next("vm", b"provider-new-secret-value",
                                 owner="provider", secrets_dir=tmp_path)
    assert phase == "staged"
    assert (tmp_path / "vm.next").read_bytes() == b"provider-new-secret-value"


def test_import_next_idempotent_and_conflict(tmp_path: Path):
    (tmp_path / "vm").write_bytes(b"live")
    (tmp_path / "vm").chmod(0o600)
    rotation_begin("vm", owner="provider", secrets_dir=tmp_path)
    rotation_import_next("vm", b"secretB", owner="provider", secrets_dir=tmp_path)
    # same bytes → idempotent success
    assert rotation_import_next("vm", b"secretB", owner="provider",
                                secrets_dir=tmp_path) == "staged"
    # different bytes → conflict
    with pytest.raises(ValueError, match="secret_conflict"):
        rotation_import_next("vm", b"secretC", owner="provider",
                             secrets_dir=tmp_path)


def test_promote_replaces_live_and_clears_state(tmp_path: Path):
    old = _seed_live("vm", tmp_path)
    rotation_begin("vm", owner="casa", secrets_dir=tmp_path)
    new_next = (tmp_path / "vm.next").read_bytes()
    assert new_next != old
    phase = rotation_promote("vm", secrets_dir=tmp_path)
    assert phase == "idle"
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) == new_next
    assert not (tmp_path / "vm.next").exists()
    assert not (tmp_path / "vm.rot.json").exists()


def test_recover_staged_with_valid_next_stays_staged(tmp_path: Path):
    _seed_live("vm", tmp_path)
    rotation_begin("vm", owner="casa", secrets_dir=tmp_path)
    assert rotation_recover("vm", owner="casa", secrets_dir=tmp_path) == "staged"


def test_recover_staged_missing_next_reverts_idle(tmp_path: Path):
    _seed_live("vm", tmp_path)
    rotation_begin("vm", owner="casa", secrets_dir=tmp_path)
    (tmp_path / "vm.next").unlink()
    assert rotation_recover("vm", owner="casa", secrets_dir=tmp_path) == "idle"
    assert not (tmp_path / "vm.rot.json").exists()


def test_recover_awaiting_next_stays_awaiting(tmp_path: Path):
    (tmp_path / "vm").write_bytes(b"live")
    (tmp_path / "vm").chmod(0o600)
    rotation_begin("vm", owner="provider", secrets_dir=tmp_path)
    assert rotation_recover("vm", owner="provider",
                            secrets_dir=tmp_path) == "awaiting_next"


def test_recover_promote_midway_completes_rename(tmp_path: Path):
    _seed_live("vm", tmp_path)
    rotation_begin("vm", owner="casa", secrets_dir=tmp_path)
    new_next = (tmp_path / "vm.next").read_bytes()
    # Simulate a crash after persisting 'promote' state but before rename.
    (tmp_path / "vm.rot.json").write_text(json.dumps(
        {"phase": "promote", "secret_owner": "casa", "started_ts": 0}))
    assert rotation_recover("vm", owner="casa", secrets_dir=tmp_path) == "idle"
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) == new_next


def test_recover_malformed_state_fail_closed(tmp_path: Path):
    _seed_live("vm", tmp_path)
    (tmp_path / "vm.rot.json").write_text("{not json")
    assert rotation_recover("vm", owner="casa", secrets_dir=tmp_path) == "idle"
    assert not (tmp_path / "vm.rot.json").exists()
    # live secret still readable
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) is not None


def test_provider_crash_after_next_before_staged_idempotent_completes(tmp_path: Path):
    # Sol r7-1: crash after `.next` durably published but before `staged` state
    # persisted → recovery stays single-accept (awaiting_next); an idempotent
    # re-import of the same bytes completes the transition to staged.
    (tmp_path / "vm").write_bytes(b"live")
    (tmp_path / "vm").chmod(0o600)
    rotation_begin("vm", owner="provider", secrets_dir=tmp_path)
    from webhook_auth import _publish  # simulate the durable .next write
    _publish("vm.next", b"provider-next-value", tmp_path)
    # state still says awaiting_next (staged never persisted)
    assert rotation_recover("vm", owner="provider",
                            secrets_dir=tmp_path) == "awaiting_next"
    # idempotent re-import of the same bytes → staged
    assert rotation_import_next("vm", b"provider-next-value", owner="provider",
                                secrets_dir=tmp_path) == "staged"


def test_begin_when_next_exists_reuses_not_clobbered(tmp_path: Path):
    _seed_live("vm", tmp_path)
    rotation_begin("vm", owner="casa", secrets_dir=tmp_path)
    first_next = (tmp_path / "vm.next").read_bytes()
    # A second begin (prior unfinished rotation) reuses the existing .next.
    rotation_begin("vm", owner="casa", secrets_dir=tmp_path)
    assert (tmp_path / "vm.next").read_bytes() == first_next
