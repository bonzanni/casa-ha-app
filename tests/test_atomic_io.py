"""Tests for the shared crash-safe atomic-write helper (atomic_io.py)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import atomic_io

pytestmark = pytest.mark.unit


def test_atomic_write_text_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "out.txt"
    atomic_io.atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_atomic_write_json_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    atomic_io.atomic_write_json(p, {"b": 1, "a": 2}, indent=2, sort_keys=True)
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 2, "b": 1}
    # sort_keys honoured
    assert p.read_text(encoding="utf-8").index('"a"') < p.read_text(
        encoding="utf-8"
    ).index('"b"')


def test_crash_between_tempwrite_and_replace_keeps_original(
    tmp_path: Path, monkeypatch
) -> None:
    """Simulate a power-loss crash after the temp file is written but before
    os.replace commits it: the ORIGINAL target must be intact (not truncated),
    and no stray temp file left behind."""
    p = tmp_path / "state.json"
    p.write_text('{"old": true}', encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(atomic_io.os, "replace", boom)
    with pytest.raises(RuntimeError):
        atomic_io.atomic_write_json(p, {"new": True})

    # Original untouched.
    assert json.loads(p.read_text(encoding="utf-8")) == {"old": True}
    # No orphaned temp sidecar.
    leftovers = [f for f in os.listdir(tmp_path) if f != "state.json"]
    assert leftovers == []


def test_mode_bits_applied(tmp_path: Path) -> None:
    p = tmp_path / "secret.conf"
    atomic_io.atomic_write_text(p, "token", mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600


def test_new_file_default_mode_is_0644(tmp_path: Path) -> None:
    """A fresh atomic write with no explicit mode must land at 0o644 — the same
    perms a plain open("w") produced under the default umask. Guards against the
    tempfile.mkstemp() 0o600 leaking onto the replaced inode."""
    p = tmp_path / "state.json"
    atomic_io.atomic_write_json(p, {"a": 1})
    assert (p.stat().st_mode & 0o777) == 0o644


def test_existing_file_mode_preserved_on_rewrite(tmp_path: Path) -> None:
    """Rewriting an existing file with no explicit mode must keep the file's
    current permission bits, never downgrade them to the tempfile 0o600."""
    p = tmp_path / "state.json"
    p.write_text("{}", encoding="utf-8")
    os.chmod(p, 0o640)
    atomic_io.atomic_write_json(p, {"a": 1})
    assert (p.stat().st_mode & 0o777) == 0o640
