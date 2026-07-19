"""Per-trigger secret staging + ownership-aware validation (Release A, Task 4)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from webhook_auth import ensure_secret, read_secret


def test_casa_ensure_creates_43char_0600(tmp_path: Path):
    val = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    assert val is not None and len(val) == 43
    f = tmp_path / "vm"
    assert f.exists()
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600


def test_casa_ensure_is_idempotent(tmp_path: Path):
    a = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    b = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    assert a == b and a is not None


def test_casa_short_file_is_invalid(tmp_path: Path):
    (tmp_path / "vm").write_bytes(b"tooshort")
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) is None


def test_symlink_final_name_rejected(tmp_path: Path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"x" * 43)
    os.symlink(target, tmp_path / "vm")
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) is None


def test_provider_ensure_readonly_none_when_absent(tmp_path: Path):
    assert ensure_secret("vm", owner="provider", secrets_dir=tmp_path) is None


def test_provider_accepts_opaque_value(tmp_path: Path):
    opaque = b"whsec_" + b"A1b2C3" * 30  # ~186 bytes, printable ASCII
    (tmp_path / "vm").write_bytes(opaque)
    os.chmod(tmp_path / "vm", 0o600)
    assert read_secret("vm", owner="provider", secrets_dir=tmp_path) == opaque


def test_provider_rejects_empty_and_oversize(tmp_path: Path):
    (tmp_path / "empty").write_bytes(b"")
    assert read_secret("empty", owner="provider", secrets_dir=tmp_path) is None
    (tmp_path / "big").write_bytes(b"a" * 5000)
    assert read_secret("big", owner="provider", secrets_dir=tmp_path) is None


def test_provider_rejects_non_printable(tmp_path: Path):
    (tmp_path / "np").write_bytes(b"abc\x00def")
    assert read_secret("np", owner="provider", secrets_dir=tmp_path) is None


def test_missing_secret_reads_none(tmp_path: Path):
    assert read_secret("nope", owner="casa", secrets_dir=tmp_path) is None


def test_orphan_tmp_files_swept(tmp_path: Path):
    orphan = tmp_path / ".tmp-999-oldjunk"
    orphan.write_bytes(b"junk")
    # Backdate mtime beyond the 60s sweep window.
    old = orphan.stat().st_mtime - 120
    os.utime(orphan, (old, old))
    ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    assert not orphan.exists()


# ---------------------------------------------------------------------------
# Release B — artifact retirement: retire_secret removes ALL slots
# ---------------------------------------------------------------------------


def test_retire_secret_removes_live_next_and_rotation_state(tmp_path):
    from webhook_auth import retire_secret, rotation_begin

    name = "plg-elevenlabs--voicemail"
    ensure_secret(name, owner="casa", secrets_dir=tmp_path)
    rotation_begin(name, owner="casa", secrets_dir=tmp_path)
    assert (tmp_path / name).exists()
    assert (tmp_path / f"{name}.next").exists()
    assert (tmp_path / f"{name}.rot.json").exists()

    retire_secret(name, secrets_dir=tmp_path)
    assert not (tmp_path / name).exists()
    assert not (tmp_path / f"{name}.next").exists()
    assert not (tmp_path / f"{name}.rot.json").exists()
    # and a later mint starts FRESH (no inheritance)
    fresh = ensure_secret(name, owner="casa", secrets_dir=tmp_path)
    assert fresh is not None


def test_retire_secret_tolerates_missing_files_and_dir(tmp_path):
    from webhook_auth import retire_secret

    retire_secret("never-existed", secrets_dir=tmp_path)          # no raise
    retire_secret("x", secrets_dir=tmp_path / "no-such-dir")      # no raise
    retire_secret("", secrets_dir=tmp_path)                       # no raise


def test_retire_secrets_with_prefix_sweeps_all_slots(tmp_path):
    """Sol shipB-r1 P1-4: revoke retires from the FILESYSTEM inventory by
    prefix — live + .next + .rot.json for every matching base, others kept."""
    from webhook_auth import retire_secrets_with_prefix, rotation_begin

    for base in ("plg-p--a", "plg-p--b", "plg-other--x", "resident-vm"):
        ensure_secret(base, owner="casa", secrets_dir=tmp_path)
    rotation_begin("plg-p--a", owner="casa", secrets_dir=tmp_path)

    retired = retire_secrets_with_prefix("plg-p--", secrets_dir=tmp_path)
    assert retired == ["plg-p--a", "plg-p--b"]
    assert not (tmp_path / "plg-p--a").exists()
    assert not (tmp_path / "plg-p--a.next").exists()
    assert not (tmp_path / "plg-p--a.rot.json").exists()
    assert not (tmp_path / "plg-p--b").exists()
    assert (tmp_path / "plg-other--x").exists()
    assert (tmp_path / "resident-vm").exists()


def test_retire_secrets_with_prefix_tolerates_missing_dir(tmp_path):
    from webhook_auth import retire_secrets_with_prefix

    assert retire_secrets_with_prefix("plg-p--",
                                      secrets_dir=tmp_path / "nope") == []
    assert retire_secrets_with_prefix("", secrets_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# Terra shipB-r2: identity-bound minting — non-inheritance enforced at
# ACTIVATION, independent of whether an earlier retirement succeeded.
# ---------------------------------------------------------------------------


def test_identity_bound_mint_stable_for_same_identity(tmp_path):
    from webhook_auth import ensure_secret_for_identity

    a = ensure_secret_for_identity("plg-p--t", identity="i1",
                                   secrets_dir=tmp_path)
    b = ensure_secret_for_identity("plg-p--t", identity="i1",
                                   secrets_dir=tmp_path)
    assert a is not None and a == b


def test_identity_change_rekeys_even_if_retire_was_skipped(tmp_path):
    """The original P1-4 scenario: the old artifact's secret SURVIVED (a
    failed/skipped retirement); the new identity's activation must never
    reuse it."""
    from webhook_auth import ensure_secret_for_identity

    old = ensure_secret_for_identity("plg-p--t", identity="old-artifact",
                                     secrets_dir=tmp_path)
    new = ensure_secret_for_identity("plg-p--t", identity="new-artifact",
                                     secrets_dir=tmp_path)
    assert new is not None and new != old


def test_unbound_existing_secret_is_rekeyed(tmp_path):
    """A live secret with no .ident sidecar (pre-Release-B mint, crash, or a
    handler lazy mint) has unknown provenance — rekey, never reuse."""
    from webhook_auth import ensure_secret_for_identity

    legacy = ensure_secret("plg-p--t", owner="casa", secrets_dir=tmp_path)
    bound = ensure_secret_for_identity("plg-p--t", identity="i1",
                                       secrets_dir=tmp_path)
    assert bound is not None and bound != legacy


def test_rekey_failure_fails_closed_to_none(tmp_path, monkeypatch):
    """If the stale secret cannot actually be removed, activation returns
    None (trigger stays unrouted with trigger_secret_missing) — NEVER the
    surviving old credential."""
    import webhook_auth
    from webhook_auth import ensure_secret_for_identity

    ensure_secret_for_identity("plg-p--t", identity="old",
                               secrets_dir=tmp_path)
    monkeypatch.setattr(webhook_auth, "retire_secret",
                        lambda name, *, secrets_dir: None)  # retire no-ops
    assert ensure_secret_for_identity("plg-p--t", identity="new",
                                      secrets_dir=tmp_path) is None


def test_retire_removes_identity_sidecar_too(tmp_path):
    from webhook_auth import ensure_secret_for_identity, retire_secret

    ensure_secret_for_identity("plg-p--t", identity="i1",
                               secrets_dir=tmp_path)
    assert (tmp_path / "plg-p--t.ident").exists()
    retire_secret("plg-p--t", secrets_dir=tmp_path)
    assert not (tmp_path / "plg-p--t.ident").exists()


def test_prefix_retirement_covers_ident_sidecars(tmp_path):
    from webhook_auth import (ensure_secret_for_identity,
                              retire_secrets_with_prefix)

    ensure_secret_for_identity("plg-p--t", identity="i1",
                               secrets_dir=tmp_path)
    retired = retire_secrets_with_prefix("plg-p--", secrets_dir=tmp_path)
    assert retired == ["plg-p--t"]
    assert not (tmp_path / "plg-p--t.ident").exists()
