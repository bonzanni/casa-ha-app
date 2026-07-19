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
import json
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
# no leading/trailing/interior whitespace and no extra fields. Both fields are
# LENGTH-BOUNDED so an attacker cannot force a huge ``int()`` (a 5000-digit
# timestamp would otherwise raise before auth → 500): a unix timestamp is ~10
# digits, and a SHA-256 hex digest is 64 chars.
_TS_RE = re.compile(r"^t=(\d{1,19}),v0=([0-9a-f]{1,128})$")


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


def retire_secret(name: str, *, secrets_dir: Path) -> None:
    """Remove EVERY slot for ``name`` — the live secret, a staged ``.next``,
    and the rotation state file (Release B artifact retirement).

    Called when the owning plugin artifact changes or is removed, BEFORE any
    re-approval can mint a replacement — a new artifact never inherits the
    old one's credentials. Missing files/dir are fine; never raises.
    """
    if not name:
        return
    secrets_dir = Path(secrets_dir)
    for fname in (name, _next_name(name), f"{name}.rot.json"):
        try:
            (secrets_dir / fname).unlink()
        except OSError:
            pass
    try:
        _fsync_dir(secrets_dir)
    except OSError:
        pass


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


# ---------------------------------------------------------------------------
# Secret rotation state machine (spec A2c) — persisted, crash-safe.
#
# Phases (state file ``<name>.rot.json``; absent = idle):
#   awaiting_next : provider rotation begun, waiting for the provider `.next`
#                   import (single-accept; live endpoint keeps working).
#   staged        : `.next` present + valid; verifier dual-accepts live + next.
#   promote       : persisted immediately before the live-replace rename, so a
#                   crash mid-rename is recoverable.
#
# `.next` is published with the same no-clobber staging primitive as the live
# secret; the state file is published with an atomic overwrite (os.replace).
# ---------------------------------------------------------------------------


def _state_path(name: str, secrets_dir: Path) -> Path:
    return secrets_dir / f"{name}.rot.json"


def _next_name(name: str) -> str:
    return f"{name}.next"


def _write_state(name: str, phase: str, owner: str, secrets_dir: Path) -> None:
    payload = json.dumps(
        {"phase": phase, "secret_owner": owner, "started_ts": int(time.time())}
    ).encode("ascii")
    tmp = secrets_dir / f".rot-{os.getpid()}-{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, _state_path(name, secrets_dir))
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_state(name: str, secrets_dir: Path) -> dict | None:
    """Parsed rotation state, or ``None`` if absent/malformed (fail-closed:
    a malformed state file is deleted)."""
    path = _state_path(name, secrets_dir)
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        st = json.loads(raw)
        if not isinstance(st, dict) or st.get("phase") not in (
            "awaiting_next", "staged", "promote"
        ):
            raise ValueError("bad phase")
        return st
    except (ValueError, TypeError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _clear_state(name: str, secrets_dir: Path) -> None:
    try:
        _state_path(name, secrets_dir).unlink()
    except OSError:
        pass
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _fsync_dir(secrets_dir: Path) -> None:
    dir_fd = os.open(secrets_dir, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def rotation_begin(name: str, *, owner: str, secrets_dir: Path) -> str:
    """Begin a rotation. Returns the resolved phase.

    ``casa``: mint (or reuse an existing) ``.next`` and go straight to
    ``staged``. ``provider``: enter ``awaiting_next`` (the ``.next`` arrives
    later via :func:`rotation_import_next`).
    """
    secrets_dir = Path(secrets_dir)
    if owner == "casa":
        # Reuse an existing `.next` (prior unfinished rotation), else mint.
        if _read_final(_next_name(name), owner, secrets_dir) is None:
            _publish(_next_name(name),
                     secrets.token_urlsafe(_CASA_TOKEN_NBYTES).encode("ascii"),
                     secrets_dir)
        _write_state(name, "staged", owner, secrets_dir)
        return "staged"
    _write_state(name, "awaiting_next", owner, secrets_dir)
    return "awaiting_next"


def rotation_import_next(
    name: str, value: bytes, *, owner: str, secrets_dir: Path,
) -> str:
    """Import a provider-minted ``.next`` secret (slot=next). Transitions
    ``awaiting_next`` → ``staged``. Idempotent for an equal re-import; raises
    ``ValueError('secret_conflict')`` for an unequal one (spec A2b/Sol r5-1)."""
    secrets_dir = Path(secrets_dir)
    if not _valid_value(owner, value):
        raise ValueError("invalid_secret_value")
    existing = _read_final(_next_name(name), owner, secrets_dir)
    if existing is not None:
        if not hmac.compare_digest(existing, value):
            raise ValueError("secret_conflict")
        _write_state(name, "staged", owner, secrets_dir)
        return "staged"
    _publish(_next_name(name), value, secrets_dir)
    _write_state(name, "staged", owner, secrets_dir)
    return "staged"


def rotation_promote(name: str, *, secrets_dir: Path) -> str:
    """Promote ``.next`` to the live secret and clear rotation state.

    Persists ``promote`` before the rename so a crash mid-rename recovers.
    """
    secrets_dir = Path(secrets_dir)
    st = _read_state(name, secrets_dir)
    owner = (st or {}).get("secret_owner", "casa")
    _write_state(name, "promote", owner, secrets_dir)
    os.replace(secrets_dir / _next_name(name), secrets_dir / name)
    _fsync_dir(secrets_dir)
    _clear_state(name, secrets_dir)
    return "idle"


def rotation_recover(name: str, *, owner: str, secrets_dir: Path) -> str:
    """Reconcile persisted rotation state at boot. Returns the resolved phase.

    Every durable combination converges: ``awaiting_next`` keeps waiting;
    ``staged`` stays only with a valid ``.next`` (else reverts to idle);
    ``promote`` completes the rename if ``.next`` survives, else the live file
    already won.
    """
    secrets_dir = Path(secrets_dir)
    st = _read_state(name, secrets_dir)
    if st is None:
        return "idle"
    phase = st["phase"]
    st_owner = st.get("secret_owner", owner)
    if phase == "awaiting_next":
        return "awaiting_next"
    if phase == "staged":
        if _read_final(_next_name(name), st_owner, secrets_dir) is not None:
            return "staged"
        _clear_state(name, secrets_dir)
        return "idle"
    # promote
    next_path = secrets_dir / _next_name(name)
    if next_path.exists():
        os.replace(next_path, secrets_dir / name)
        _fsync_dir(secrets_dir)
    _clear_state(name, secrets_dir)
    return "idle"
