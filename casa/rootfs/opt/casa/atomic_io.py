"""Crash-safe atomic file writes.

Route on-disk state writes (registries, tombstones, manifests) through these
helpers so a crash or power loss mid-write can never leave a truncated or
partially-written file. Each helper writes to a temporary file *in the same
directory* as the target — so :func:`os.replace` is a same-filesystem atomic
rename, not a cross-device copy — flushes and ``os.fsync``s the temp file's
data to disk, then ``os.replace``s it over the target.

Deliberately tiny and dependency-free (stdlib only): these are called from
sync code, often via :func:`asyncio.to_thread`. If the write fails at any
point before the final replace, the original target file is left untouched
and the temp file is cleaned up.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Atomically write *text* to *path*.

    Writes a sidecar temp file in the same directory, fsyncs it, then
    ``os.replace``s it over *path*. When *mode* is given, the target file's
    permission bits are set to it (applied to the temp file before the
    replace so the mode is in effect the instant the file appears).

    When *mode* is ``None`` the prior ``open("w")`` permission semantics are
    preserved: rewriting an existing file keeps that file's current mode, and
    a fresh file lands at ``0o644``. This is necessary because
    :func:`tempfile.mkstemp` creates the sidecar at ``0o600`` and
    :func:`os.replace` adopts the temp inode — without this the atomic write
    would silently downgrade every replaced file to ``0o600``.
    """
    target = os.fspath(path)
    directory = os.path.dirname(target) or "."
    if mode is None:
        try:
            mode = os.stat(target).st_mode & 0o777
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Any failure before the replace leaves the original intact; drop
        # the orphaned temp file so a crashed write can't litter the dir.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Atomically write *data* as JSON to *path* (see :func:`atomic_write_text`)."""
    atomic_write_text(
        path,
        json.dumps(data, indent=indent, sort_keys=sort_keys),
        encoding=encoding,
        mode=mode,
    )
