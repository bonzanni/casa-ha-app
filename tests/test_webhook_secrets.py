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
