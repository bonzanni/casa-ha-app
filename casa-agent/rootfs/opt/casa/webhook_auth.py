"""Per-trigger webhook auth verification (Release A).

Leaf module — stdlib only (plus ``log_redact`` for secret registration, wired
by callers). Pure verifiers: no IO, no clock access (``now`` is injected so the
handler owns time and tests stay deterministic).

Three modes (spec A1):

* ``hmac_body``       — hex HMAC-SHA256 of the raw body, header
  ``X-Webhook-Signature`` by default. Uses the global ``WEBHOOK_SECRET``.
* ``static_header``   — constant-time compare of a header value against the
  per-trigger secret (ElevenLabs agent tools, n8n).
* ``timestamped_hmac``— ``t=<unix>,v0=<hex>`` where
  ``v0 = HMAC_SHA256(secret, "{t}.{body}")`` (ElevenLabs post-call, Stripe-
  style), gated by a tolerance window.

All comparisons are constant-time on bytes; a non-ASCII or malformed header
yields ``False`` (→ 401 at the handler), never an exception (→ 500). This
mirrors the L4 lesson in the Telegram update handler.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Mapping

# Casa-minted secrets: 32 urlsafe bytes → exactly 43 base64url chars.
_CASA_TOKEN_NBYTES = 32
_CASA_TOKEN_LEN = 43
# Provider (opaque) secret bounds (spec A2, Sol r4-4).
_PROVIDER_MAX = 4096
# Orphan staging-file sweep window.
_TMP_SWEEP_SECS = 60

# Strict single-instance parse: exactly ``t=<digits>,v0=<lowercase-hex>`` with
# no leading/trailing/interior whitespace and no extra fields.
_TS_RE = re.compile(r"^t=(\d+),v0=([0-9a-f]+)$")


def _ct_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def _header_ascii_bytes(headers: Mapping[str, str], name: str) -> bytes | None:
    """The header value as ASCII bytes, ``None`` if absent, ``b""`` if it holds
    a non-ASCII value (which then guarantees a constant-time mismatch rather
    than raising on ``compare_digest``)."""
    v = headers.get(name)
    if v is None:
        return None
    try:
        return v.encode("ascii")
    except UnicodeEncodeError:
        return b""


def verify(
    mode: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    secret: bytes,
    header_name: str,
    tolerance_secs: int,
    now: int,
) -> bool:
    """Return whether the request authenticates under ``mode``.

    Fail-closed: an empty ``secret`` never authenticates.
    """
    if not secret:
        return False

    if mode == "hmac_body":
        got = _header_ascii_bytes(headers, header_name)
        if got is None:
            return False
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest().encode("ascii")
        return _ct_eq(got, expected)

    if mode == "static_header":
        got = _header_ascii_bytes(headers, header_name)
        return got is not None and _ct_eq(got, secret)

    if mode == "timestamped_hmac":
        raw = headers.get(header_name)
        if raw is None:
            return False
        try:
            raw.encode("ascii")
        except UnicodeEncodeError:
            return False
        m = _TS_RE.match(raw)
        if not m:
            return False
        t = int(m.group(1))
        if abs(now - t) > tolerance_secs:
            return False
        expected = hmac.new(
            secret, f"{t}.".encode() + body, hashlib.sha256,
        ).hexdigest()
        return _ct_eq(m.group(2).encode("ascii"), expected.encode("ascii"))

    return False


# ---------------------------------------------------------------------------
# Per-trigger secret storage (spec A2) — crash-safe staging, ownership-aware
# validation. All reads/writes are fail-closed: an invalid or unreadable slot
# yields ``None`` and the caller omits the trigger (never an open pass-through).
# ---------------------------------------------------------------------------


def _valid_value(owner: str, raw: bytes) -> bool:
    """Owner-appropriate validation of a stored secret's bytes."""
    if owner == "casa":
        return len(raw) == _CASA_TOKEN_LEN and raw.isascii()
    # provider: opaque, non-empty, bounded, printable ASCII.
    if not raw or len(raw) > _PROVIDER_MAX:
        return False
    return all(0x20 <= b < 0x7F for b in raw)


def _sweep_orphans(secrets_dir: Path) -> None:
    now = time.time()
    for p in secrets_dir.glob(".tmp-*"):
        try:
            if now - p.stat().st_mtime > _TMP_SWEEP_SECS:
                p.unlink()
        except OSError:
            pass


def _read_final(name: str, owner: str, secrets_dir: Path) -> bytes | None:
    """Read and owner-validate the live secret; ``None`` on any anomaly.

    ``O_NOFOLLOW`` rejects a symlinked final name; ``fstat`` rejects a
    non-regular file.
    """
    path = secrets_dir / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        raw = os.read(fd, _PROVIDER_MAX + 1)
    finally:
        os.close(fd)
    return raw if _valid_value(owner, raw) else None


def _publish(name: str, value: bytes, secrets_dir: Path) -> None:
    """Atomically publish ``value`` at ``name`` via staging + linkat.

    The final name never holds a partial file: a private staging file is
    written and fsynced, then hard-linked into place (``EEXIST`` if a
    concurrent winner already published — never clobbered).
    """
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = secrets_dir / f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
    fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, value)
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.link(staging, secrets_dir / name)
        except FileExistsError:
            pass  # a concurrent winner published first — keep theirs
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
        try:
            staging.unlink()
        except OSError:
            pass


def read_secret(name: str, *, owner: str, secrets_dir: Path) -> bytes | None:
    """Read the live secret for ``name`` (fail-closed)."""
    return _read_final(name, owner, Path(secrets_dir))


def ensure_secret(name: str, *, owner: str, secrets_dir: Path) -> bytes | None:
    """Return the live secret for ``name``, minting it for ``owner="casa"`` if
    absent. For ``owner="provider"`` this is read-only (Casa never mints a
    provider secret) — returns ``None`` until imported.
    """
    secrets_dir = Path(secrets_dir)
    if secrets_dir.exists():
        _sweep_orphans(secrets_dir)
    existing = _read_final(name, owner, secrets_dir)
    if existing is not None:
        return existing
    if owner != "casa":
        return None
    _publish(name, secrets.token_urlsafe(_CASA_TOKEN_NBYTES).encode("ascii"),
             secrets_dir)
    return _read_final(name, owner, secrets_dir)
