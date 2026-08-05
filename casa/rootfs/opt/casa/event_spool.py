"""The ``/data/events`` spool — plugin-emitted domain events, casa-minted
generations, and per-subscriber delivery receipts (INV-EV-001..006).

A plugin emits a named event ("something happened in my data"); casa folds
queued emissions into a generation, mints one delivery record per
consented subscriber, and redelivers until an ack or exhaustion. This
module owns every durable artifact of that protocol — layout, the fold
(reconstruct/repair/open), the conditional delivery update, the ack
verbs, the sweep (watermark + disk valve + quarantine + tombstone +
removal), and the removal-record ledger.

Filesystem discipline (0o770 dirs / 0o600 files, ``.dir-id`` identity,
atomic temp+rename+fsync, unique staging) and most of the low-level FD
plumbing below are a direct mirror of ``callback_spool.py`` — read that
module's docstring and ``:73-206``/``:885-960``/``:1173-1366``/``:1907``/
``:3368-3660`` before touching this one. Two protocol differences from
callbacks explain the rest of the shape:

* **No claim/publish handshake.** An emission is casa-owned from the
  moment it lands (``emit()`` writes it directly); there is no consumer
  pickup to arbitrate, so there is no ``Claim``/``PublishOutcome``
  analogue here at all — only the fold pass ever mutates an emission
  file (moves it out of existence by folding it) or a delivery record.
* **Level-triggered, not exactly-once.** A wake carries no data — the
  subscriber's own durable state is the real queue — so a lost,
  suppressed or duplicate wake costs promptness, never correctness. That
  is why fold's crash recovery is pure idempotent replay (reconstruct +
  repair) rather than a write-ahead-then-delete dance keyed on proving a
  deletion durable first.

**Serialization.** One synchronous ``threading.RLock`` (``_lock``) guards
every state/delivery read-merge-write. Every method here is synchronous;
an async caller (the worker, a later task) reaches this spool via
``asyncio.to_thread``. Multi-file passes (``fold_pass``, ``sweep``,
``recovery_pass``) are worker-only by convention — this module enforces
nothing about which thread calls them, exactly like ``callback_spool``.

**Layout** (``/data/events`` by default, ``CASA_EVENT_SPOOL_ROOT``
overrides)::

    .removals/<plugin>-<uuid>.json
    <emitter>/
      .dir-id
      ready.json
      emissions/<event>--<u32hex>.json
      emissions/.part-<u32hex>
      state/<event>.json
      state/.corrupt-<ts>-<event>
      delivery/<event>--<subscriber>.json
      delivery/.corrupt-<ts>-<name>

``emitter`` is a plugin registry name and is the ONLY plugin identity
folded into a path component at that level — ``event`` (an emitter's own
declared event name, validated by ``event_attempts._valid_event``'s
grammar restated on read via ``event_attempts.validate_record``) never
contains ``--``, so every ``<event>--<rest>`` filename splits
unambiguously on its FIRST ``--`` regardless of what ``rest`` (a
subscriber name, a random hex token) itself contains.

**The ``routed`` contract.** Every method that takes a ``routed``
parameter accepts either the module sentinel :data:`ROUTING_UNAVAILABLE`
(the caller's routing compute failed or has not yet run — see the
spec's decision 26) or a ``dict[(emitter, event), set[subscriber]]``
mapping a pair to its currently-consented, currently-routed subscriber
names. This is deliberately narrower than the reconciler's own routed
map (which additionally carries each subscriber's consented artifact/
target snapshot for the dispatch-time identity gate, spec decision 27) —
this module only ever needs COHORT membership, never consent identity;
the identity re-check belongs to the worker's pre-send gate, a later
task. Under the sentinel, :meth:`EventSpool.fold_pass` is a strict no-op
in every phase and :meth:`EventSpool.sweep` degrades to part-TTL
housekeeping only — no destructive action ever runs against a routed
view this module cannot prove authoritative.
"""
from __future__ import annotations

import errno
import fcntl
import json
import logging
import math
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import event_attempts

logger = logging.getLogger(__name__)

SPOOL_ROOT_ENV = "CASA_EVENT_SPOOL_ROOT"
ROOT = Path("/data/events")

SKEW_S = 300                  # future-mtime allowance; beyond it: fail closed
TEMP_TTL_S = 300              # `.part-`/staged-replace residue age-sweep
QUIESCENCE_S = 24 * 3600      # orphan-dir GC floor
FOLD_BATCH_MAX = 64           # per-generation fold batch bound (oldest first)
MAX_EMISSION_FILES = 512      # disk-pressure valve, per emitter
REMOVAL_RECORD_PRUNE_S = 7 * 24 * 3600     # a NOTED removal record is kept a
#                              week, then pruned
REMOVAL_RECORD_MAX_AGE_S = 30 * 24 * 3600  # hard bound, noted or not
MARKER_STATE_MAX_BYTES = 1 << 16

STATE_SCHEMA_VERSION = 1
REMOVAL_SCHEMA_VERSION = 1

EMISSIONS_DIR = "emissions"
STATE_DIR = "state"
DELIVERY_DIR = "delivery"
REMOVALS_DIR = ".removals"
DIR_ID_NAME = ".dir-id"
READY_NAME = "ready.json"
INDEX_DIR = ".index"
PART_PREFIX = ".part-"
CORRUPT_PREFIX = ".corrupt-"
REPLACE_TEMP_INFIX = ".tmp-"

DIR_MODE = 0o770
FILE_MODE = 0o600

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = (os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
                   | os.O_CLOEXEC)
_MARKER_READ_FLAGS = (os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                      | os.O_CLOEXEC)

_DIR_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_U32HEX_RE = re.compile(r"^[0-9a-f]{8}$")

_STATE_KEYS = frozenset({"v", "event", "gen", "cohort", "folded", "opened_ts"})
_REMOVAL_KEYS = frozenset({"v", "plugin", "ts", "entries", "noted", "noted_ts"})


class _RoutingUnavailable:
    """The type of :data:`ROUTING_UNAVAILABLE` — a unique sentinel, never
    constructed a second time, never equal to anything but itself."""

    __slots__ = ()

    def __repr__(self) -> str:                          # pragma: no cover
        return "ROUTING_UNAVAILABLE"


#: Published when the caller's routing compute is unavailable (failed, or
#: has not yet run) — spec decision 26. Distinguished from an
#: authoritative EMPTY ``{}`` map: an empty map is a real (if currently
#: routeless) compute result and authorizes full destructive sweeping;
#: this sentinel authorizes none.
ROUTING_UNAVAILABLE = _RoutingUnavailable()


class SpoolClosed(RuntimeError):
    """Raised by administrative operations on a closed spool."""


class MarkerState:
    ABSENT = "absent"
    INVALID = "invalid"
    PRESENT = "present"


@dataclass(frozen=True)
class Marker:
    state: str
    payload: "dict | None" = None


@dataclass
class SweepReport:
    deleted_temps: int = 0
    deleted_watermark: int = 0
    deleted_valve: int = 0
    quarantined_delivery: int = 0
    terminalized: int = 0
    dropped_records: int = 0
    dropped_corrupt: int = 0
    removal_records_written: int = 0
    anomalies: list = field(default_factory=list)


@dataclass
class RecoveryReport:
    opened: list = field(default_factory=list)
    sweep: "SweepReport | None" = None
    gc_removed: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# names / keys
# ---------------------------------------------------------------------------


def canonical_marker_bytes(payload: dict) -> bytes:
    """The ONE canonical on-disk form of a durable record — sorted keys,
    compact separators, no ASCII-escaping, UTF-8. Mirrors
    ``callback_spool.canonical_marker_bytes`` exactly, including
    ``allow_nan=False`` (a non-finite float must never be written as the
    non-standard ``NaN``/``Infinity`` JSON literal)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def spool_root() -> Path:
    return Path(os.environ.get(SPOOL_ROOT_ENV) or ROOT)


def _safe_component(name) -> bool:
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if "/" in name or "\0" in name:
        return False
    return not any(ord(c) < 0x20 for c in name)


def _split_effective(name_no_ext: str) -> "tuple[str, str] | None":
    """Split a ``<event>--<rest>`` stem on the FIRST ``--``. Safe
    regardless of what ``rest`` itself contains: ``event`` is validated
    (by :func:`event_attempts.validate_record`) to never contain ``--``,
    so the first occurrence is always exactly the separator."""
    idx = name_no_ext.find("--")
    if idx <= 0:
        return None
    return name_no_ext[:idx], name_no_ext[idx + 2:]


def _is_replace_temp(name: str) -> bool:
    return name.startswith(".") and REPLACE_TEMP_INFIX in name


def _is_clock(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _is_removal_name(name: str) -> bool:
    return (_safe_component(name) and not name.startswith(".")
            and name.endswith(".json"))


def _validate_removal_entry(e) -> bool:
    if not isinstance(e, dict):
        return False
    kind = e.get("kind")
    if kind == "record":
        return (set(e) == {"kind", "emitter", "event", "gen"}
                and isinstance(e["emitter"], str) and e["emitter"]
                and isinstance(e["event"], str) and e["event"]
                and isinstance(e["gen"], int) and not isinstance(e["gen"], bool)
                and e["gen"] >= 0)
    if kind == "corrupt":
        return (set(e) == {"kind", "file"}
                and isinstance(e["file"], str) and e["file"])
    return False


def _validate_removal(obj) -> "dict | None":
    """Total fail-closed validation of a removal record — a copy, or
    ``None``. Mirrors ``callback_spool._validate_removal``'s discipline
    (exact key set, a consistency gate between ``noted``/``noted_ts``),
    with ``entries`` (a non-empty tagged-union list, spec decision 38)
    standing in for callback's ``count``/``reason``."""
    try:
        if not isinstance(obj, dict) or set(obj) != _REMOVAL_KEYS:
            return None
        if isinstance(obj["v"], bool) or obj["v"] != REMOVAL_SCHEMA_VERSION:
            return None
        plugin = obj["plugin"]
        if not isinstance(plugin, str) or not _safe_component(plugin) \
                or plugin.startswith("."):
            return None
        if not _is_clock(obj["ts"]):
            return None
        entries = obj["entries"]
        if not isinstance(entries, list) or not entries:
            return None
        if not all(_validate_removal_entry(e) for e in entries):
            return None
        if not isinstance(obj["noted"], bool):
            return None
        noted_ts = obj["noted_ts"]
        if noted_ts is not None and not _is_clock(noted_ts):
            return None
        if obj["noted"] != (noted_ts is not None):
            return None
        return dict(obj)
    except Exception:  # noqa: BLE001 — total by contract
        return None


def _validate_state(obj, *, expect_event: "str | None" = None) -> "dict | None":
    """Total fail-closed validation of a generation-state record — a
    copy, or ``None``. Never raises: a scribbled/corrupt file must read
    as invalid so :meth:`EventSpool.fold_pass` reconstructs it, never as
    truth."""
    try:
        if not isinstance(obj, dict) or set(obj) != _STATE_KEYS:
            return None
        if isinstance(obj["v"], bool) or obj["v"] != STATE_SCHEMA_VERSION:
            return None
        event = obj["event"]
        if not isinstance(event, str) or not event:
            return None
        if expect_event is not None and event != expect_event:
            return None
        gen = obj["gen"]
        if isinstance(gen, bool) or not isinstance(gen, int) or gen < 1:
            return None
        cohort = obj["cohort"]
        if not isinstance(cohort, list) \
                or not all(isinstance(s, str) and s for s in cohort):
            return None
        if len(set(cohort)) != len(cohort):
            return None
        folded = obj["folded"]
        if not isinstance(folded, list) \
                or not all(isinstance(u, str) and _U32HEX_RE.match(u)
                          for u in folded):
            return None
        if not _is_clock(obj["opened_ts"]):
            return None
        return dict(obj)
    except Exception:  # noqa: BLE001 — total by contract
        return None


# ---------------------------------------------------------------------------
# low-level fd helpers — mirror callback_spool.py (:338-659) exactly
# ---------------------------------------------------------------------------


def _fsync(fd: int, what: str) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.warning("event-spool: fsync of %s failed: %s", what, exc)


class FsyncFailed(OSError):
    """An fsync whose failure the caller must observe (the generation
    state write depends on it being proven durable before any dependent
    record upsert or emission unlink)."""


def _fsync_strict(fd: int, what: str) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise FsyncFailed(
            exc.errno if exc.errno is not None else errno.EIO,
            f"fsync of {what} failed") from exc


def _open_dir(name: str, dir_fd: int) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _lstat_quiet(name: str, dir_fd: int):
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return None


def _listdir_quiet(dir_fd: int) -> list:
    try:
        return os.listdir(dir_fd)
    except OSError:
        return []


def _unlink_quiet(name: str, dir_fd: int) -> bool:
    try:
        os.unlink(name, dir_fd=dir_fd)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("event-spool: unlink failed (errno %s)", exc.errno)
        return False


def _remove_entry(name: str, dir_fd: int, st) -> bool:
    try:
        if st is not None and stat.S_ISDIR(st.st_mode):
            shutil.rmtree(name, dir_fd=dir_fd)
            return True
    except OSError as exc:
        logger.warning("event-spool: rmtree failed (errno %s)", exc.errno)
        return False
    return _unlink_quiet(name, dir_fd)


def _write_new_file(name: str, dir_fd: int, data: bytes) -> None:
    fd = os.open(name, _NEW_FILE_FLAGS, FILE_MODE, dir_fd=dir_fd)
    try:
        os.fchmod(fd, FILE_MODE)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        _fsync(fd, "staged file")
    finally:
        os.close(fd)


_TOKEN_ERROR: object = object()
_SCAN_ERROR: object = object()


def _classify_dir_token(dir_fd: int):
    try:
        fd = os.open(DIR_ID_NAME, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        return _TOKEN_ERROR
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != 32:
            return None
        chunks = bytearray()
        while len(chunks) < 33:
            piece = os.read(fd, 33 - len(chunks))
            if not piece:
                break
            chunks += piece
        if len(chunks) != 32:
            return _TOKEN_ERROR
    except OSError:
        return _TOKEN_ERROR
    finally:
        os.close(fd)
    token = chunks.decode("ascii", errors="replace")
    return token if _DIR_ID_RE.match(token) else None


def _retire_marker_entry(name: str, dir_fd: int) -> bool:
    st = _lstat_quiet(name, dir_fd)
    if st is None:
        return True
    _remove_entry(name, dir_fd, st)
    return _lstat_quiet(name, dir_fd) is None


def _read_marker_at(name: str, dir_fd: int) -> Marker:
    """Total, non-blocking, three-state marker read openat-relative to
    *dir_fd*. Mirrors ``callback_spool._read_marker_at`` exactly."""
    try:
        fd = os.open(name, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return Marker(MarkerState.ABSENT)
    except OSError:
        return Marker(MarkerState.INVALID)
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return Marker(MarkerState.INVALID)
        if not stat.S_ISREG(st.st_mode):
            return Marker(MarkerState.INVALID)
        chunks: list = []
        size = 0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return Marker(MarkerState.INVALID)
            if not chunk:
                break
            size += len(chunk)
            if size > MARKER_STATE_MAX_BYTES:
                return Marker(MarkerState.INVALID)
            chunks.append(chunk)
    finally:
        os.close(fd)
    body = b"".join(chunks)
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 — total, see callback_spool precedent
        return Marker(MarkerState.INVALID)
    if not isinstance(obj, dict):
        return Marker(MarkerState.INVALID)
    return Marker(MarkerState.PRESENT, obj)


def _strict_replace_at(name: str, dir_fd: int, data: bytes, *, parent_fd: int,
                       what: str, parent_what: str, role: str) -> bool:
    """Staged replace with STRICT durability — mirrors
    ``callback_spool._strict_replace_at`` exactly. Used for every write a
    dependent action (a record upsert, an emission unlink, a purge)
    keys on: the generation-state file and the removal-record ledger."""
    tmp = f".{name}{REPLACE_TEMP_INFIX}{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fd = os.open(tmp, _NEW_FILE_FLAGS, FILE_MODE, dir_fd=dir_fd)
        try:
            os.fchmod(fd, FILE_MODE)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            _fsync_strict(fd, f"staged {role}")
        finally:
            os.close(fd)
    except OSError as exc:
        _unlink_quiet(tmp, dir_fd)
        logger.warning("event-spool: %s staging failed (errno %s)",
                       role, exc.errno)
        return False
    try:
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        _unlink_quiet(tmp, dir_fd)
        logger.warning("event-spool: %s publish failed (errno %s)",
                       role, exc.errno)
        return False
    try:
        _fsync_strict(dir_fd, what)
        _fsync_strict(parent_fd, parent_what)
    except FsyncFailed as exc:
        logger.warning("event-spool: %s dir fsync failed (errno %s)",
                       role, exc.errno)
        return False
    return True


def _replace_json(name: str, dir_fd: int, payload: dict, what: str) -> None:
    """Best-effort replacing publish — for the advisory ``ready.json`` /
    index entries and for delivery-record upserts (self-healing via
    :meth:`EventSpool.fold_pass`'s REPAIR phase covers a lost update;
    nothing's deletion depends on a delivery write alone)."""
    data = canonical_marker_bytes(payload)
    tmp = f".{name}{REPLACE_TEMP_INFIX}{os.getpid()}-{uuid.uuid4().hex}"
    _write_new_file(tmp, dir_fd, data)
    try:
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        _unlink_quiet(tmp, dir_fd)
        raise
    _fsync(dir_fd, what)


# ---------------------------------------------------------------------------
# consumer-side reference — the executable half of the emission contract
# ---------------------------------------------------------------------------


def emit(emitter_dir: "Path | str", event: str) -> Path:
    """Mint a fresh emission — the CONSUMER's (emitting plugin's) whole
    half of the protocol, kept here as the executable reference.

    The envelope is the canonical bytes of exactly ``{"v": 1}`` (spec
    §4) — an event emission carries no consumer-authored payload through
    the spool; it is a pure wake-up. Written to a unique
    ``.part-<u32hex>`` (0600, fsynced), then renamed to
    ``emissions/<event>--<u32hex>.json``. **No arbitration**: the
    8-hex-char suffix (``os.urandom(4)``) is fresh per call, so two
    concurrent emitters never contend for the same name — there is
    nothing here for a ``link(2)``/``EEXIST`` arbiter to decide, unlike
    ``callback_spool.mint``'s hash-of-consumer-state naming. Returns the
    published path.
    """
    if not _safe_component(event) or "--" in event:
        raise ValueError(f"unsafe event name {event!r}")
    envelope = canonical_marker_bytes({"v": event_attempts.SCHEMA_VERSION})
    token = os.urandom(4).hex()
    emissions = Path(emitter_dir) / EMISSIONS_DIR
    part = f"{PART_PREFIX}{token}"
    final = f"{event}--{token}.json"
    dir_fd = os.open(emissions, _DIR_FLAGS)
    try:
        _write_new_file(part, dir_fd, envelope)
        try:
            os.rename(part, final, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            _unlink_quiet(part, dir_fd)
            raise
        _fsync(dir_fd, str(emissions))
    finally:
        os.close(dir_fd)
    return emissions / final


# ---------------------------------------------------------------------------
# the spool
# ---------------------------------------------------------------------------


class EventSpool:
    """One instance, owned by casa-core, pinned to the spool root. See the
    module docstring for the layout and the ``routed`` contract every
    multi-file pass shares."""

    def __init__(self, root: "Path | str") -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._closed = False
        os.makedirs(self.root, mode=DIR_MODE, exist_ok=True)
        self._root_fd = os.open(os.path.realpath(self.root), _DIR_FLAGS)
        try:
            os.fchmod(self._root_fd, DIR_MODE)
        except OSError as exc:                          # pragma: no cover
            logger.warning("event-spool: chmod of root failed: %s", exc)

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.close(self._root_fd)
            except OSError:
                pass
            self._root_fd = -1

    def _require_open(self) -> None:
        if self._closed:
            raise SpoolClosed("event spool is closed")

    # -- directories ------------------------------------------------------

    def ensure_emitter_dirs(self, emitter: str) -> None:
        """Create ``<emitter>/{emissions,state,delivery}`` at 0770 and
        mint/repair ``.dir-id``. Idempotent. Mirrors
        ``callback_spool.CallbackSpool.ensure_plugin_dirs`` exactly."""
        if not _safe_component(emitter) or emitter.startswith("."):
            raise ValueError(f"unsafe emitter spool name {emitter!r}")
        with self._lock:
            self._require_open()
            self._mkdir(emitter, self._root_fd)
            efd = _open_dir(emitter, self._root_fd)
            try:
                self._chmod_dir(efd, emitter)
                for sub in (EMISSIONS_DIR, STATE_DIR, DELIVERY_DIR):
                    self._mkdir(sub, efd)
                    sfd = _open_dir(sub, efd)
                    try:
                        self._chmod_dir(sfd, sub)
                    finally:
                        os.close(sfd)
                probe = _classify_dir_token(efd)
                if probe is _TOKEN_ERROR:
                    raise OSError(errno.EIO, f"{DIR_ID_NAME} state unknowable")
                if probe is None:
                    self._repair_dir_token(efd)
                _fsync(efd, emitter)
            finally:
                os.close(efd)
            _fsync(self._root_fd, str(self.root))

    @staticmethod
    def _repair_dir_token(efd: int) -> None:
        fcntl.flock(efd, fcntl.LOCK_EX)
        try:
            probe = _classify_dir_token(efd)
            if probe is _TOKEN_ERROR:
                raise OSError(errno.EIO, f"{DIR_ID_NAME} state unknowable")
            if probe is None:
                if not _retire_marker_entry(DIR_ID_NAME, efd):
                    raise OSError(errno.EIO,
                                  f"invalid {DIR_ID_NAME} survives retire")
                _write_new_file(DIR_ID_NAME, efd,
                                uuid.uuid4().hex.encode("ascii"))
        finally:
            fcntl.flock(efd, fcntl.LOCK_UN)

    @staticmethod
    def _mkdir(name: str, dir_fd: int) -> bool:
        try:
            os.mkdir(name, DIR_MODE, dir_fd=dir_fd)
            return True
        except FileExistsError:
            return False

    @staticmethod
    def _chmod_dir(fd: int, what: str) -> None:
        try:
            os.fchmod(fd, DIR_MODE)
        except OSError as exc:                          # pragma: no cover
            logger.warning("event-spool: chmod of %s failed: %s", what, exc)

    def _emitter_fd(self, emitter: str) -> int:
        if not _safe_component(emitter):
            raise ValueError(f"unsafe emitter spool name {emitter!r}")
        return _open_dir(emitter, self._root_fd)

    def _emitter_dirs(self) -> list:
        out = []
        for name in _listdir_quiet(self._root_fd):
            if name.startswith(".") or not _safe_component(name):
                continue
            st = _lstat_quiet(name, self._root_fd)
            if st is not None and stat.S_ISDIR(st.st_mode):
                out.append(name)
        return sorted(out)

    # -- readiness marker + discovery index (mirror callback_spool) -----

    def write_ready(self, emitter: str, payload: dict) -> None:
        with self._lock:
            self._require_open()
            efd = self._emitter_fd(emitter)
            try:
                _replace_json(READY_NAME, efd, payload, emitter)
            finally:
                os.close(efd)

    def delete_ready(self, emitter: str) -> bool:
        with self._lock:
            self._require_open()
            try:
                efd = self._emitter_fd(emitter)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            try:
                gone = _retire_marker_entry(READY_NAME, efd)
                _fsync(efd, emitter)
                return gone
            finally:
                os.close(efd)

    def read_marker(self, emitter: str) -> Marker:
        with self._lock:
            if self._closed or not _safe_component(emitter):
                return Marker(MarkerState.ABSENT)
            try:
                efd = self._emitter_fd(emitter)
            except FileNotFoundError:
                return Marker(MarkerState.ABSENT)
            except (OSError, ValueError):
                return Marker(MarkerState.INVALID)
            try:
                return _read_marker_at(READY_NAME, efd)
            finally:
                os.close(efd)

    def _index_fd(self, *, create: bool) -> int:
        if create and self._mkdir(INDEX_DIR, self._root_fd):
            _fsync(self._root_fd, str(self.root))
        fd = _open_dir(INDEX_DIR, self._root_fd)
        if create:
            self._chmod_dir(fd, INDEX_DIR)
        return fd

    def write_index_entry(self, key: str, payload: dict) -> None:
        with self._lock:
            self._require_open()
            ifd = self._index_fd(create=True)
            try:
                _replace_json(f"{key}.json", ifd, payload, INDEX_DIR)
            finally:
                os.close(ifd)

    def read_index_marker(self, key: str) -> Marker:
        with self._lock:
            if self._closed:
                return Marker(MarkerState.ABSENT)
            try:
                ifd = _open_dir(INDEX_DIR, self._root_fd)
            except FileNotFoundError:
                return Marker(MarkerState.ABSENT)
            except OSError:
                return Marker(MarkerState.INVALID)
            try:
                return _read_marker_at(f"{key}.json", ifd)
            finally:
                os.close(ifd)

    def delete_index_entry(self, key: str) -> bool:
        with self._lock:
            self._require_open()
            try:
                ifd = self._index_fd(create=False)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            try:
                gone = _retire_marker_entry(f"{key}.json", ifd)
                _fsync(ifd, INDEX_DIR)
                return gone
            finally:
                os.close(ifd)

    def index_keys(self) -> list:
        with self._lock:
            if self._closed:
                return []
            try:
                ifd = _open_dir(INDEX_DIR, self._root_fd)
            except OSError:
                return []
            try:
                out = []
                for name in _listdir_quiet(ifd):
                    if _is_replace_temp(name):
                        continue
                    if name.endswith(".json"):
                        out.append(name[:-5])
                return sorted(out)
            finally:
                os.close(ifd)

    def published_emitters(self) -> list:
        with self._lock:
            if self._closed:
                return []
            out = []
            for emitter in self._emitter_dirs():
                try:
                    efd = self._emitter_fd(emitter)
                except (OSError, ValueError):
                    continue
                try:
                    if _lstat_quiet(READY_NAME, efd) is not None:
                        out.append(emitter)
                finally:
                    os.close(efd)
            return out

    # -- generation-state I/O --------------------------------------------

    def _read_state_marker(self, sfd: int, event: str) -> Marker:
        return _read_marker_at(f"{event}.json", sfd)

    def _write_state(self, efd: int, event: str, obj: dict) -> bool:
        """Strict-durable write of the generation-state anchor. Every
        dependent action (a REPAIR upsert, an OPEN unlink) in
        :meth:`fold_pass` runs only after this proves durable — the
        anchor must exist before anything that leans on it does."""
        try:
            data = canonical_marker_bytes(obj)
        except (TypeError, ValueError):
            return False
        try:
            sfd = _open_dir(STATE_DIR, efd)
        except OSError:
            return False
        try:
            return _strict_replace_at(
                f"{event}.json", sfd, data, parent_fd=efd,
                what=STATE_DIR, parent_what="emitter", role="state")
        finally:
            os.close(sfd)

    def _quarantine_state(self, efd: int, event: str, now: float) -> bool:
        try:
            sfd = _open_dir(STATE_DIR, efd)
        except OSError:
            return False
        try:
            qname = self._unique_quarantine_name(sfd, event, now)
            if qname is None:
                return False
            try:
                os.rename(f"{event}.json", qname, src_dir_fd=sfd, dst_dir_fd=sfd)
            except OSError:
                return False
            _fsync(sfd, STATE_DIR)
            return True
        finally:
            os.close(sfd)

    @staticmethod
    def _unique_quarantine_name(dir_fd: int, tail: str, now: float) -> "str | None":
        qname = f"{CORRUPT_PREFIX}{int(now)}-{tail}"
        n = 1
        while _lstat_quiet(qname, dir_fd) is not None:
            n += 1
            qname = f"{CORRUPT_PREFIX}{int(now)}-{n}-{tail}"
            if n > 1000:                                 # pragma: no cover
                return None
        return qname

    def read_state(self, emitter: str, event: str) -> "dict | None":
        """The current, validated generation-state record for a pair, or
        ``None`` (absent or corrupt) — a read-only convenience for
        callers (and tests) that never mutates anything."""
        with self._lock:
            if self._closed:
                return None
            try:
                efd = self._emitter_fd(emitter)
            except (OSError, ValueError):
                return None
            try:
                try:
                    sfd = _open_dir(STATE_DIR, efd)
                except OSError:
                    return None
                try:
                    marker = self._read_state_marker(sfd, event)
                finally:
                    os.close(sfd)
            finally:
                os.close(efd)
            if marker.state is not MarkerState.PRESENT:
                return None
            return _validate_state(marker.payload, expect_event=event)

    # -- delivery-record I/O ---------------------------------------------

    def _read_valid_delivery_records(self, dfd: int, emitter: str,
                                     event: str) -> dict:
        out = {}
        prefix = f"{event}--"
        for name in _listdir_quiet(dfd):
            if name.startswith(".") or not name.endswith(".json") \
                    or not name.startswith(prefix):
                continue
            subscriber = name[len(prefix):-5]
            marker = _read_marker_at(name, dfd)
            if marker.state is not MarkerState.PRESENT:
                continue
            rec = event_attempts.validate_record(
                marker.payload, expect_emitter=emitter, expect_event=event,
                expect_subscriber=subscriber)
            if rec is not None:
                out[subscriber] = rec
        return out

    def _write_delivery(self, dfd: int, event: str, subscriber: str,
                        rec: dict) -> bool:
        try:
            _replace_json(f"{event}--{subscriber}.json", dfd, rec, DELIVERY_DIR)
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("event-spool: delivery write failed (errno %s)",
                           getattr(exc, "errno", None))
            return False

    def read_delivery(self, emitter: str, event: str,
                      subscriber: str) -> "dict | None":
        with self._lock:
            if self._closed:
                return None
            try:
                efd = self._emitter_fd(emitter)
            except (OSError, ValueError):
                return None
            try:
                try:
                    dfd = _open_dir(DELIVERY_DIR, efd)
                except OSError:
                    return None
                try:
                    marker = _read_marker_at(f"{event}--{subscriber}.json", dfd)
                finally:
                    os.close(dfd)
            finally:
                os.close(efd)
            if marker.state is not MarkerState.PRESENT:
                return None
            return event_attempts.validate_record(
                marker.payload, expect_emitter=emitter, expect_event=event,
                expect_subscriber=subscriber)

    # -- emission listing / unlink ----------------------------------------

    def _list_emissions_for_event(self, emfd: int, event: str) -> list:
        prefix = f"{event}--"
        out = []
        for name in _listdir_quiet(emfd):
            if name.startswith(PART_PREFIX) or not name.endswith(".json") \
                    or not name.startswith(prefix):
                continue
            st = _lstat_quiet(name, emfd)
            if st is None or not stat.S_ISREG(st.st_mode):
                continue
            out.append((st.st_mtime, name))
        return out

    @staticmethod
    def _u32hex_of(event: str, name: str) -> "str | None":
        token = name[len(event) + 2:-5]
        return token if _U32HEX_RE.match(token) else None

    def list_emissions(self, emitter: str, event: str) -> list:
        """Basenames of an event's current emission files — a read-only
        convenience for tests and callers."""
        with self._lock:
            if self._closed:
                return []
            try:
                efd = self._emitter_fd(emitter)
            except (OSError, ValueError):
                return []
            try:
                try:
                    emfd = _open_dir(EMISSIONS_DIR, efd)
                except OSError:
                    return []
                try:
                    return sorted(n for _, n in
                                 self._list_emissions_for_event(emfd, event))
                finally:
                    os.close(emfd)
            finally:
                os.close(efd)

    # -- fold: reconstruct -> repair -> open, per (emitter, event) -------

    def _candidate_events(self, efd: int) -> set:
        events = set()
        try:
            sfd = _open_dir(STATE_DIR, efd)
            try:
                for name in _listdir_quiet(sfd):
                    if name.endswith(".json") and not name.startswith("."):
                        events.add(name[:-5])
            finally:
                os.close(sfd)
        except OSError:
            pass
        try:
            emfd = _open_dir(EMISSIONS_DIR, efd)
            try:
                for name in _listdir_quiet(emfd):
                    if name.startswith(PART_PREFIX) or not name.endswith(".json"):
                        continue
                    parsed = _split_effective(name[:-5])
                    if parsed:
                        events.add(parsed[0])
            finally:
                os.close(emfd)
        except OSError:
            pass
        try:
            dfd = _open_dir(DELIVERY_DIR, efd)
            try:
                for name in _listdir_quiet(dfd):
                    if name.startswith(".") or not name.endswith(".json"):
                        continue
                    parsed = _split_effective(name[:-5])
                    if parsed:
                        events.add(parsed[0])
            finally:
                os.close(dfd)
        except OSError:
            pass
        return events

    def fold_pass(self, routed, now: float) -> list:
        """Reconstruct + repair + open, per ``(emitter, event)`` pair
        with any on-disk trace (a state file, an emission, or a delivery
        record). Returns every ``(emitter, event, subscriber)`` whose
        delivery record was freshly minted or refreshed this pass (the
        worker's dispatch worklist).

        Under :data:`ROUTING_UNAVAILABLE` this is a STRICT no-op — no
        read past the sentinel check, no reconstruction, no repair, no
        folded-unlink, no open (Terra-r6 #2 pin)."""
        if routed is ROUTING_UNAVAILABLE:
            return []
        opened = []
        with self._lock:
            if self._closed:
                return []
            for emitter in self._emitter_dirs():
                try:
                    efd = self._emitter_fd(emitter)
                except (OSError, ValueError):
                    continue
                try:
                    events = self._candidate_events(efd)
                    for event in sorted(events):
                        pair_routed = set((routed or {}).get((emitter, event)) or set())
                        try:
                            opened.extend(self._fold_one(
                                emitter, efd, event, pair_routed, now))
                        except Exception:  # noqa: BLE001 — one bad pair must
                            # never abort the rest of the pass
                            logger.warning(
                                "event-spool: fold of %r/%r failed",
                                emitter, event, exc_info=True)
                finally:
                    os.close(efd)
        return opened

    def _fold_one(self, emitter: str, efd: int, event: str,
                  routed_subs: set, now: float) -> list:
        changed = []
        sfd = _open_dir(STATE_DIR, efd)
        dfd = _open_dir(DELIVERY_DIR, efd)
        emfd = _open_dir(EMISSIONS_DIR, efd)
        try:
            state_marker = self._read_state_marker(sfd, event)
            valid_records = self._read_valid_delivery_records(dfd, emitter, event)

            # -- RECONSTRUCT (decision 25) --------------------------------
            state_obj = None
            if state_marker.state is MarkerState.PRESENT:
                state_obj = _validate_state(state_marker.payload, expect_event=event)
            need_reconstruct = state_obj is None
            if need_reconstruct and valid_records:
                max_gen = max(r["gen"] for r in valid_records.values())
                cohort = sorted(sub for sub, r in valid_records.items()
                                if r["gen"] == max_gen)
                new_state = {"v": STATE_SCHEMA_VERSION, "event": event,
                            "gen": max_gen, "cohort": cohort, "folded": [],
                            "opened_ts": now}
                if state_marker.state is not MarkerState.ABSENT:
                    self._quarantine_state(efd, event, now)
                if self._write_state(efd, event, new_state):
                    state_obj = new_state
            # else (need_reconstruct and not valid_records): nothing
            # survives to rebuild from — silent, no issue (decision 37).
            # state_obj stays None; OPEN below mints generation 1 the
            # ordinary way once its own preconditions are met.

            # -- REPAIR (before any idle decision, decision 24) ----------
            if state_obj is not None:
                state_gen = state_obj["gen"]
                cohort = set(state_obj["cohort"])
                eligible = cohort & routed_subs
                for sub in sorted(eligible):
                    rec = valid_records.get(sub)
                    if rec is None or rec["gen"] < state_gen:
                        tok = uuid.uuid4().hex
                        newrec = event_attempts.new_record(
                            emitter, event, sub, state_gen, tok, now)
                        if self._write_delivery(dfd, event, sub, newrec):
                            valid_records[sub] = newrec
                            changed.append((emitter, event, sub))
                for u in state_obj["folded"]:
                    if _U32HEX_RE.match(u):
                        _unlink_quiet(f"{event}--{u}.json", emfd)

            # -- OPEN ------------------------------------------------------
            cur_gen = state_obj["gen"] if state_obj is not None else 0
            emissions = self._list_emissions_for_event(emfd, event)
            idle = not any(r["status"] == "pending"
                          for r in valid_records.values())
            if emissions and routed_subs and idle:
                oldest = sorted(emissions, key=lambda t: (t[0], t[1]))[:FOLD_BATCH_MAX]
                new_gen = cur_gen + 1
                new_cohort = sorted(routed_subs)
                folded_tokens = []
                for _, name in oldest:
                    tok = self._u32hex_of(event, name)
                    if tok is not None:
                        folded_tokens.append(tok)
                new_state = {"v": STATE_SCHEMA_VERSION, "event": event,
                            "gen": new_gen, "cohort": new_cohort,
                            "folded": folded_tokens, "opened_ts": now}
                if self._write_state(efd, event, new_state):
                    for sub in new_cohort:
                        tok = uuid.uuid4().hex
                        newrec = event_attempts.new_record(
                            emitter, event, sub, new_gen, tok, now)
                        if self._write_delivery(dfd, event, sub, newrec):
                            changed.append((emitter, event, sub))
                    for _, name in oldest:
                        _unlink_quiet(name, emfd)
        finally:
            os.close(sfd)
            os.close(dfd)
            os.close(emfd)
        return changed

    # -- conditional delivery update (mirrors update_attempt_nudge) ------

    def update_delivery_nudge(self, emitter: str, event: str, subscriber: str,
                              gen: int, mutator) -> bool:
        """Locked read-merge-write of one delivery record. ``False``
        unless the on-disk record is readable, valid, ``status ==
        "pending"`` AND its ``gen`` matches — a done record is immutable
        forever, and a generation that has already rotated refuses a
        stale caller's update outright (the fold's CAS).

        *mutator* receives a COPY of the current record and returns the
        mutated dict; only ``nudges``, ``last_nudge_ts``, ``next_nudge_ts``,
        ``deferrals``, ``noted``, ``status``, ``outcome`` and ``ended_ts``
        may differ from the input — any other change (identity, ``gen``,
        ``ack_token``, ``minted_ts``) is refused. Exhaustion is exactly
        one such call: a mutator that sets ``status="done",
        outcome="exhausted", noted=True`` writes all three atomically."""
        allowed = frozenset({"nudges", "last_nudge_ts", "next_nudge_ts",
                             "deferrals", "noted", "status", "outcome",
                             "ended_ts"})
        with self._lock:
            rec = self.read_delivery(emitter, event, subscriber)
            if rec is None:
                return False
            if rec["status"] != "pending" or rec["gen"] != gen:
                return False
            try:
                merged = mutator(dict(rec))
            except Exception:  # noqa: BLE001 — a broken mutator must not
                # corrupt the record or escape into the caller
                logger.warning("event-spool: delivery-nudge mutator raised",
                               exc_info=True)
                return False
            if not isinstance(merged, dict):
                return False
            changed = {k for k in set(rec) | set(merged)
                      if rec.get(k) != merged.get(k)}
            if not changed <= allowed:
                return False
            validated = event_attempts.validate_record(
                merged, expect_emitter=emitter, expect_event=event,
                expect_subscriber=subscriber)
            if validated is None or validated["gen"] != gen:
                return False
            try:
                efd = self._emitter_fd(emitter)
            except (OSError, ValueError):
                return False
            try:
                try:
                    dfd = _open_dir(DELIVERY_DIR, efd)
                except OSError:
                    return False
                try:
                    return self._write_delivery(dfd, event, subscriber, validated)
                finally:
                    os.close(dfd)
            finally:
                os.close(efd)

    # -- ack ---------------------------------------------------------------

    def ack(self, emitter: str, event: str, token: str,
           now: "float | None" = None) -> tuple:
        """``("acked", subscriber)`` on a pending current-record token
        match (rewritten ``done``/``acked``); ``("already_done",
        subscriber)`` when the token matches a DONE record (no mutation
        — an idempotent re-ack); ``("no_match", None)`` otherwise (a
        stale/rotated token, a quarantined record, or an unknown pair —
        no mutation)."""
        if now is None:
            now = time.time()
        with self._lock:
            if self._closed or not _safe_component(emitter) \
                    or not isinstance(token, str) or not token:
                return ("no_match", None)
            try:
                efd = self._emitter_fd(emitter)
            except FileNotFoundError:
                return ("no_match", None)
            except (OSError, ValueError):
                return ("no_match", None)
            try:
                try:
                    dfd = _open_dir(DELIVERY_DIR, efd)
                except OSError:
                    return ("no_match", None)
                try:
                    prefix = f"{event}--"
                    for name in sorted(_listdir_quiet(dfd)):
                        if name.startswith(".") or not name.endswith(".json") \
                                or not name.startswith(prefix):
                            continue
                        subscriber = name[len(prefix):-5]
                        marker = _read_marker_at(name, dfd)
                        if marker.state is not MarkerState.PRESENT:
                            continue
                        rec = event_attempts.validate_record(
                            marker.payload, expect_emitter=emitter,
                            expect_event=event, expect_subscriber=subscriber)
                        if rec is None or rec["ack_token"] != token:
                            continue
                        if rec["status"] == "done":
                            return ("already_done", rec["subscriber"])
                        newrec = event_attempts.terminalize(rec, "acked", now=now)
                        self._write_delivery(dfd, event, rec["subscriber"], newrec)
                        return ("acked", rec["subscriber"])
                    return ("no_match", None)
                finally:
                    os.close(dfd)
            finally:
                os.close(efd)

    # -- sweep ---------------------------------------------------------------

    @staticmethod
    def _expired(st, now: float, ttl: float) -> bool:
        return st.st_mtime > now + SKEW_S or now - st.st_mtime > ttl

    def sweep(self, routed, installed: set, registry_valid: bool,
             now: float) -> SweepReport:
        """Authoritative-or-sentinel (spec decision 26): under
        :data:`ROUTING_UNAVAILABLE` this pass does part-TTL housekeeping
        ONLY — no deletion, no terminalization, no removal record.
        Authoritative passes additionally: delete every emission file of
        a pair with no routed subscriber (the consent watermark, decision
        23); enforce the disk-pressure valve; quarantine invalid delivery
        files; terminalize unrouted records (retained as tombstones); and
        DROP a subscriber's tombstoned records — durable removal record
        first — once that subscriber's own plugin is no longer
        installed. State files are never touched here."""
        report = SweepReport()
        with self._lock:
            if self._closed:
                return report
            if routed is ROUTING_UNAVAILABLE:
                for emitter in self._emitter_dirs():
                    try:
                        efd = self._emitter_fd(emitter)
                    except (OSError, ValueError):
                        continue
                    try:
                        self._sweep_part_temps(efd, now, report)
                    finally:
                        os.close(efd)
                return report
            installed = set(installed or ())
            to_drop: dict = {}
            deletable: dict = {}
            for emitter in self._emitter_dirs():
                try:
                    efd = self._emitter_fd(emitter)
                except (OSError, ValueError):
                    continue
                try:
                    self._sweep_part_temps(efd, now, report)
                    self._sweep_emissions(emitter, efd, routed, now, report)
                    self._sweep_delivery(emitter, efd, routed, installed, now,
                                        report, to_drop, deletable)
                finally:
                    os.close(efd)
            self._drop_removed_subscribers(to_drop, deletable, now, report)
        return report

    def _sweep_part_temps(self, efd: int, now: float, report: SweepReport) -> None:
        try:
            emfd = _open_dir(EMISSIONS_DIR, efd)
        except OSError:
            return
        try:
            removed = False
            for name in _listdir_quiet(emfd):
                if not name.startswith(PART_PREFIX):
                    continue
                st = _lstat_quiet(name, emfd)
                if st is None:
                    continue
                if not stat.S_ISREG(st.st_mode) or self._expired(st, now, TEMP_TTL_S):
                    if _remove_entry(name, emfd, st):
                        report.deleted_temps += 1
                        removed = True
            if removed:
                _fsync(emfd, EMISSIONS_DIR)
        finally:
            os.close(emfd)

    def _sweep_emissions(self, emitter: str, efd: int, routed: dict,
                         now: float, report: SweepReport) -> None:
        try:
            emfd = _open_dir(EMISSIONS_DIR, efd)
        except OSError:
            return
        try:
            keep = []
            touched = False
            for name in _listdir_quiet(emfd):
                if name.startswith(PART_PREFIX) or not name.endswith(".json"):
                    continue
                parsed = _split_effective(name[:-5])
                if parsed is None:
                    continue          # anomalous name — accepted residual,
                    # same-uid threat model (never written by this module)
                event = parsed[0]
                st = _lstat_quiet(name, emfd)
                if st is None or not stat.S_ISREG(st.st_mode):
                    continue
                routed_subs = routed.get((emitter, event)) or set()
                if not routed_subs:
                    # the watermark: no routed subscriber means the pair's
                    # whole queue is deleted, every authoritative pass —
                    # this IS decision 23, not a TTL.
                    if _unlink_quiet(name, emfd):
                        report.deleted_watermark += 1
                        touched = True
                    continue
                keep.append((st.st_mtime, name))
            if len(keep) > MAX_EMISSION_FILES:
                overflow = len(keep) - MAX_EMISSION_FILES
                victims = sorted(keep)[:overflow]
                for _, name in victims:
                    if _unlink_quiet(name, emfd):
                        report.deleted_valve += 1
                        touched = True
                logger.warning(
                    "event-spool: disk-pressure valve deleted %d oldest "
                    "emission file(s) for emitter %r (over MAX_EMISSION_FILES=%d)",
                    len(victims), emitter, MAX_EMISSION_FILES)
            if touched:
                _fsync(emfd, EMISSIONS_DIR)
        finally:
            os.close(emfd)

    def _sweep_delivery(self, emitter: str, efd: int, routed: dict,
                        installed: set, now: float, report: SweepReport,
                        to_drop: dict, deletable: dict) -> None:
        try:
            dfd = _open_dir(DELIVERY_DIR, efd)
        except OSError:
            return
        try:
            touched = False
            for name in sorted(_listdir_quiet(dfd)):
                if _is_replace_temp(name):
                    st = _lstat_quiet(name, dfd)
                    if st is not None and now - st.st_mtime > TEMP_TTL_S:
                        touched |= _unlink_quiet(name, dfd)
                    continue
                if name.startswith(CORRUPT_PREFIX):
                    continue          # already quarantined; only the
                    # subscriber-removal drop below ever removes these
                if name.startswith(".") or not name.endswith(".json"):
                    continue
                parsed = _split_effective(name[:-5])
                marker = _read_marker_at(name, dfd)
                if marker.state is MarkerState.ABSENT:
                    continue          # vanished mid-scan
                rec = None
                if parsed is not None and marker.state is MarkerState.PRESENT:
                    event, subscriber = parsed
                    rec = event_attempts.validate_record(
                        marker.payload, expect_emitter=emitter,
                        expect_event=event, expect_subscriber=subscriber)
                if rec is None:
                    qname = self._unique_quarantine_name(dfd, name, now)
                    if qname is not None:
                        try:
                            os.rename(name, qname, src_dir_fd=dfd, dst_dir_fd=dfd)
                        except OSError:
                            qname = None
                    if qname is not None:
                        report.quarantined_delivery += 1
                        touched = True
                        if parsed is not None and parsed[1] not in installed:
                            subscriber = parsed[1]
                            to_drop.setdefault(subscriber, []).append(
                                {"kind": "corrupt", "file": qname})
                            deletable.setdefault(subscriber, []).append(
                                {"kind": "corrupt", "emitter": emitter,
                                 "file": qname})
                    continue
                routed_subs = routed.get((emitter, rec["event"])) or set()
                is_routed = rec["subscriber"] in routed_subs
                if not is_routed and rec["status"] == "pending":
                    outcome = ("removed" if rec["subscriber"] not in installed
                              else "revoked")
                    newrec = event_attempts.terminalize(rec, outcome, now=now)
                    if self._write_delivery(dfd, rec["event"], rec["subscriber"],
                                            newrec):
                        rec = newrec
                        report.terminalized += 1
                        touched = True
                if rec["subscriber"] not in installed:
                    to_drop.setdefault(rec["subscriber"], []).append(
                        {"kind": "record", "emitter": emitter,
                         "event": rec["event"], "gen": rec["gen"]})
                    deletable.setdefault(rec["subscriber"], []).append(
                        {"kind": "record", "emitter": emitter,
                         "event": rec["event"], "subscriber": rec["subscriber"]})
            if touched:
                _fsync(dfd, DELIVERY_DIR)
        finally:
            os.close(dfd)

    def _drop_removed_subscribers(self, to_drop: dict, deletable: dict,
                                  now: float, report: SweepReport) -> None:
        """Strict-durable removal record BEFORE any drop (decision 33):
        a subscriber's tombstoned records and attributable quarantine
        files are physically removed only once the ledger entry naming
        them is proven durable; a record that will not go durable defers
        the whole subscriber's drop to the next pass."""
        for subscriber, entries in to_drop.items():
            if not entries:
                continue
            if not self._write_removal_record(subscriber, entries, now=now):
                logger.warning(
                    "event-spool: removal record for %r would not go "
                    "durable — drop deferred", subscriber)
                continue
            report.removal_records_written += 1
            for item in deletable.get(subscriber, []):
                try:
                    efd = self._emitter_fd(item["emitter"])
                except (OSError, ValueError):
                    continue
                try:
                    try:
                        dfd = _open_dir(DELIVERY_DIR, efd)
                    except OSError:
                        continue
                    try:
                        if item["kind"] == "record":
                            name = f'{item["event"]}--{item["subscriber"]}.json'
                        else:
                            name = item["file"]
                        if _unlink_quiet(name, dfd):
                            if item["kind"] == "record":
                                report.dropped_records += 1
                            else:
                                report.dropped_corrupt += 1
                        _fsync(dfd, DELIVERY_DIR)
                    finally:
                        os.close(dfd)
                finally:
                    os.close(efd)

    # -- recovery ------------------------------------------------------------

    def recovery_pass(self, routed, installed: set, registry_valid: bool,
                      now: float, boot: bool = False) -> RecoveryReport:
        """Reconstruct + repair + sweep from disk — the crash-recovery
        entry point (worker startup, periodic convergence). ``fold_pass``
        already IS reconstruct+repair (and, when preconditions allow,
        open — which is the correct outcome for recovery too: an OPEN
        that was interrupted converges the same way a routine pass
        would)."""
        opened = self.fold_pass(routed, now)
        swept = self.sweep(routed, installed, registry_valid, now)
        gc_removed = []
        if boot and registry_valid:
            gc_removed = self.gc_orphan_dirs(
                registry_valid=registry_valid, member_plugins=installed, now=now)
        return RecoveryReport(opened=opened, sweep=swept, gc_removed=gc_removed)

    # -- issues ----------------------------------------------------------

    def spool_issues(self) -> list:
        """Currently-quarantined artifacts, one ``event_spool_issue`` dict
        each — a live scan of disk, not a log replay."""
        issues = []
        with self._lock:
            if self._closed:
                return []
            for emitter in self._emitter_dirs():
                try:
                    efd = self._emitter_fd(emitter)
                except (OSError, ValueError):
                    continue
                try:
                    try:
                        sfd = _open_dir(STATE_DIR, efd)
                        try:
                            for name in _listdir_quiet(sfd):
                                if name.startswith(CORRUPT_PREFIX):
                                    issues.append({
                                        "reason": "event_spool_issue",
                                        "kind": "corrupt_state",
                                        "emitter": emitter, "file": name})
                        finally:
                            os.close(sfd)
                    except OSError:
                        pass
                    try:
                        dfd = _open_dir(DELIVERY_DIR, efd)
                        try:
                            for name in _listdir_quiet(dfd):
                                if name.startswith(CORRUPT_PREFIX):
                                    issues.append({
                                        "reason": "event_spool_issue",
                                        "kind": "corrupt_delivery",
                                        "emitter": emitter, "file": name})
                        finally:
                            os.close(dfd)
                    except OSError:
                        pass
                finally:
                    os.close(efd)
        return issues

    # -- removal-record ledger (mirror callback_spool :3368-3532) --------

    def _removals_fd(self, *, create: bool) -> int:
        if create and self._mkdir(REMOVALS_DIR, self._root_fd):
            _fsync(self._root_fd, str(self.root))
        fd = _open_dir(REMOVALS_DIR, self._root_fd)
        if create:
            self._chmod_dir(fd, REMOVALS_DIR)
        return fd

    def _write_removal_record(self, plugin: str, entries: list, *,
                              now: float) -> bool:
        rec = {"v": REMOVAL_SCHEMA_VERSION, "plugin": plugin, "ts": float(now),
               "entries": list(entries), "noted": False, "noted_ts": None}
        try:
            data = canonical_marker_bytes(rec)
        except (TypeError, ValueError):        # pragma: no cover — all plain
            logger.warning("event-spool: removal record is not "
                           "serializable (%r)", plugin)
            return False
        try:
            rfd = self._removals_fd(create=True)
        except OSError as exc:
            logger.warning("event-spool: removals store unavailable "
                           "(errno %s)", exc.errno)
            return False
        try:
            return _strict_replace_at(
                f"{plugin}-{uuid.uuid4().hex}.json", rfd, data,
                parent_fd=self._root_fd, what=REMOVALS_DIR,
                parent_what=str(self.root), role="removal record")
        finally:
            os.close(rfd)

    def list_removal_records(self) -> list:
        with self._lock:
            if self._closed:
                return []
            try:
                rfd = self._removals_fd(create=False)
            except OSError:
                return []
            try:
                out = []
                retired = False
                for name in sorted(_listdir_quiet(rfd)):
                    if not _is_removal_name(name):
                        continue
                    marker = _read_marker_at(name, rfd)
                    if marker.state is MarkerState.ABSENT:
                        continue
                    rec = (_validate_removal(marker.payload)
                          if marker.state is MarkerState.PRESENT else None)
                    if rec is None:
                        retired |= _retire_marker_entry(name, rfd)
                        logger.warning("event-spool: unreadable removal "
                                       "record retired")
                        continue
                    out.append((name, rec))
                if retired:
                    _fsync(rfd, REMOVALS_DIR)
                return out
            finally:
                os.close(rfd)

    def mark_removal_noted(self, filename: str, *, now: float) -> bool:
        if not _is_removal_name(filename):
            return False
        with self._lock:
            if self._closed:
                return False
            try:
                rfd = self._removals_fd(create=False)
            except OSError:
                return False
            try:
                marker = _read_marker_at(filename, rfd)
                rec = (_validate_removal(marker.payload)
                      if marker.state is MarkerState.PRESENT else None)
                if rec is None:
                    return False
                rec = dict(rec, noted=True, noted_ts=float(now))
                try:
                    data = canonical_marker_bytes(rec)
                except (TypeError, ValueError):   # pragma: no cover
                    return False
                return _strict_replace_at(
                    filename, rfd, data, parent_fd=self._root_fd,
                    what=REMOVALS_DIR, parent_what=str(self.root),
                    role="removal record")
            finally:
                os.close(rfd)

    def prune_removal_records(self, *, now: float) -> int:
        with self._lock:
            if self._closed:
                return 0
            try:
                rfd = self._removals_fd(create=False)
            except OSError:
                return 0
            try:
                pruned = 0
                touched = False
                for name in sorted(_listdir_quiet(rfd)):
                    if _is_replace_temp(name):
                        st = _lstat_quiet(name, rfd)
                        if st is not None and now - st.st_mtime > TEMP_TTL_S:
                            touched |= _unlink_quiet(name, rfd)
                        continue
                    if not _is_removal_name(name):
                        continue
                    marker = _read_marker_at(name, rfd)
                    rec = (_validate_removal(marker.payload)
                          if marker.state is MarkerState.PRESENT else None)
                    if rec is None:
                        continue
                    noted_age = (now - rec["noted_ts"] if rec["noted"] else None)
                    spent = (noted_age is not None
                            and noted_age > REMOVAL_RECORD_PRUNE_S)
                    aged = now - rec["ts"] > REMOVAL_RECORD_MAX_AGE_S
                    if not spent and not aged:
                        continue
                    if _unlink_quiet(name, rfd):
                        pruned += 1
                        touched = True
                if touched:
                    _fsync(rfd, REMOVALS_DIR)
                return pruned
            finally:
                os.close(rfd)

    # -- gated orphan-dir GC (mirror callback_spool gc_orphan_dirs) -------

    def gc_orphan_dirs(self, *, registry_valid: bool, member_plugins: set,
                       now: float) -> list:
        """Remove an EMITTER's whole spool tree once its plugin is no
        longer installed. Gated exactly like ``callback_spool``'s: a
        NO-OP unless ``registry_valid`` is ``True``, and a dir is removed
        only when quiescent AND its inventory can be PROVED (never a
        fail-open peek)."""
        if registry_valid is not True:
            return []
        removed = []
        with self._lock:
            if self._closed:
                return []
            for emitter in self._emitter_dirs():
                if emitter in member_plugins:
                    continue
                newest = self._newest_mtime(emitter)
                if newest is None or newest is _SCAN_ERROR:
                    continue
                if now - newest < QUIESCENCE_S:
                    continue
                entries = self._inventory_for_removal(emitter)
                if entries is _SCAN_ERROR:
                    continue
                if entries and not self._write_removal_record(
                        emitter, entries, now=now):
                    continue
                try:
                    shutil.rmtree(emitter, dir_fd=self._root_fd)
                except OSError as exc:
                    logger.warning("event-spool: orphan GC of %r failed "
                                   "(errno %s)", emitter, exc.errno)
                    continue
                _fsync(self._root_fd, str(self.root))
                removed.append(emitter)
                logger.info("event-spool: removed orphan emitter dir %r", emitter)
        return removed

    def _inventory_for_removal(self, emitter: str):
        """Every delivery record + quarantined file under an emitter's
        ``delivery/`` — the removal-record entries a whole-dir GC must
        account for. :data:`_SCAN_ERROR` when any part is unprovable."""
        try:
            efd = self._emitter_fd(emitter)
        except (OSError, ValueError):
            return []
        try:
            try:
                dfd = _open_dir(DELIVERY_DIR, efd)
            except FileNotFoundError:
                return []
            except OSError:
                return _SCAN_ERROR
            try:
                entries = []
                for name in _listdir_quiet(dfd):
                    if _is_replace_temp(name):
                        continue
                    if name.startswith(CORRUPT_PREFIX):
                        entries.append({"kind": "corrupt", "file": name})
                        continue
                    if name.startswith(".") or not name.endswith(".json"):
                        continue
                    parsed = _split_effective(name[:-5])
                    if parsed is None:
                        continue
                    event, subscriber = parsed
                    marker = _read_marker_at(name, dfd)
                    if marker.state is not MarkerState.PRESENT:
                        continue
                    rec = event_attempts.validate_record(
                        marker.payload, expect_emitter=emitter,
                        expect_event=event, expect_subscriber=subscriber)
                    if rec is not None:
                        entries.append({"kind": "record", "emitter": emitter,
                                        "event": event, "gen": rec["gen"]})
                return entries
            finally:
                os.close(dfd)
        finally:
            os.close(efd)

    def _newest_mtime(self, emitter: str):
        try:
            efd = self._emitter_fd(emitter)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return _SCAN_ERROR
        try:
            newest = self._newest_in(efd, depth=3)
        finally:
            os.close(efd)
        return _SCAN_ERROR if newest is None else newest

    def _newest_in(self, fd: int, depth: int):
        try:
            newest = os.fstat(fd).st_mtime
        except OSError:                                  # pragma: no cover
            return None
        try:
            names = os.listdir(fd)
        except OSError:
            return None
        for name in names:
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            newest = max(newest, st.st_mtime)
            if depth > 0 and stat.S_ISDIR(st.st_mode):
                try:
                    sub = _open_dir(name, fd)
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                try:
                    deeper = self._newest_in(sub, depth - 1)
                finally:
                    os.close(sub)
                if deeper is None:
                    return None
                newest = max(newest, deeper)
        return newest


# ---------------------------------------------------------------------------
# module singleton (initialised once at boot by casa-core)
# ---------------------------------------------------------------------------

_SPOOL: "EventSpool | None" = None


def init_spool(root: "Path | str | None" = None) -> EventSpool:
    global _SPOOL
    _SPOOL = EventSpool(spool_root() if root is None else root)
    logger.info("event-spool initialised at %s", _SPOOL.root)
    return _SPOOL


def get_spool() -> "EventSpool | None":
    return _SPOOL


def spool_issues() -> list:
    """Module-level convenience mirroring ``get_spool()`` degrade-quietly
    callers already use for callbacks: ``[]`` before boot wires a spool."""
    spool = get_spool()
    return spool.spool_issues() if spool is not None else []
