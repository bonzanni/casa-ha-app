"""The ``/data/callbacks`` spool — the authorization-callback protocol
(dirs/ready/index layout, claim/TTL/publish, the mint contract,
sweep/recovery; INV-CB-002).

An unauthenticated browser redirect deposits a short-lived bearer credential
(an OAuth authorization code) into this spool; an ephemeral consumer process
picks it up. Casa is the untrusted middle: it never parses what a consumer
minted and never keeps a credential longer than its own TTL. Three rules make
that safe, and every method below is written to preserve them:

**One clock — mtime.** ``rename(2)`` and ``link(2)`` preserve an inode's
mtime, so a pending file's mtime *is* its mint time and survives the claim;
a result's mtime is its final-write time (the inode records the last write,
not the publication instant). Each TTL therefore runs off its OWN file's
mtime: a 29-minute authorization flow must not have its freshly-written
result expire on arrival. Casa never reads or parses a pending file's
content — no consumer-supplied timestamps exist anywhere in the protocol. A
materially FUTURE mtime (beyond ``SKEW_S``) is fail-closed everywhere: the
entry is deleted, so a forward clock jump can never mint records that regain
validity when the clock returns.

**Publish-once, never a partial.** Nothing in ``pending/``, ``.claims/`` or
``results/`` is ever created by an overwriting rename. Every publication is a
``link(2)`` of an already-complete inode, whose ``EEXIST`` is the atomic
arbiter: the claim (``pending/<h>.json`` → ``.claims/<h>``) has exactly one
winner however many processes race it, and a replayed redirect can never
rewrite a result. A collector polling ``results/`` can never observe a short
record, because the name only ever appears already-written and fsynced.

**Fail-closed FDs.** All work is openat-relative to directory FDs opened
``O_NOFOLLOW`` from a pinned root FD, so a swapped symlink or a concurrent
plugin removal absorbs writes into an unlinked directory instead of racing a
recreation. A closed instance never falls through to a ``dir_fd=None`` op
(which would resolve against the process CWD).

Deliberately **simpler than** ``plugin_outbox``'s ``.reap`` ownership
protocol: that machinery exists for same-name republication by an untrusted
producer, which is structurally absent here — names are sha256 hashes of
fresh random states, so "the name now denotes a different, fresher inode" is
not a case that arises.

Same-uid processes are outside the threat model (as for ``plugin_outbox``):
all plugin processes run as root in one container. Dirs are 0770 and files
0600 as defence in depth, not as an inter-plugin boundary.

Stdlib-only leaf module: importable from the reconciler, the HTTP handler,
the sweeper job and a consumer test alike.
"""
from __future__ import annotations

import enum
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SPOOL_ROOT_ENV = "CASA_CALLBACK_SPOOL_ROOT"
ROOT = Path("/data/callbacks")

# TTLs and allowances — all in seconds, all measured against a
# file's OWN mtime.
SKEW_S = 300                 # future-mtime allowance; beyond it: fail closed
PENDING_TTL_S = 1800         # a minted state is claimable for 30 min
RESULT_TTL_S = 900           # a plaintext code is retained for 15 min at most
RESTORE_GRACE_S = 60         # periodic recovery never restores a young claim
TEMP_TTL_S = 300             # `.part` / `.tmp-<hash>` residue age-sweep
QUIESCENCE_S = 24 * 3600     # orphan-dir GC floor
MAX_PENDING = 256            # per-plugin caps: a buggy consumer must not
MAX_RESULTS = 256            # fill /data
MARKER_STATE_MAX_BYTES = 1 << 16   # a ready.json / index entry read back for
#                              the reconcile payload compare is tiny (a few
#                              hundred bytes); anything past this small cap is
#                              INVALID, never a large read

PENDING_DIR = "pending"
RESULTS_DIR = "results"
CLAIMS_DIR = ".claims"
INDEX_DIR = ".index"
READY_NAME = "ready.json"
DIR_ID_NAME = ".dir-id"      # per-plugin-dir identity token (see Claim)
TEMP_PREFIX = ".tmp-"        # reserved result-temp grammar under .claims/
COLLECT_PREFIX = ".collect-"  # consumer-held claim under results/
PART_SUFFIX = ".part"
REPLACE_TEMP_INFIX = ".tmp-"  # staging grammar of _replace_json: `.<name>.tmp-…`

DIR_MODE = 0o770
FILE_MODE = 0o600

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = (os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
                   | os.O_CLOEXEC)
#: Marker reads are NON-BLOCKING: O_NONBLOCK means opening a FIFO (or any other
#: pipe/device masquerading as ready.json) returns immediately instead of
#: blocking for a writer, so a hostile/garbage on-disk marker can never hang
#: the reconciler; the ``S_ISREG`` gate then rejects it before any read.
_MARKER_READ_FLAGS = (os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                      | os.O_CLOEXEC)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DIR_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class MarkerState(enum.Enum):
    """Three-state result of a durable-marker read (:meth:`CallbackSpool.
    read_marker`). ABSENT (no file) and INVALID (present but non-regular /
    unreadable / oversized / malformed) are DISTINCT: a stale-but-INVALID
    marker must be republished, never mistaken for absent (and so left to
    survive a failed rewrite)."""

    ABSENT = "absent"
    INVALID = "invalid"
    PRESENT = "present"


@dataclass(frozen=True)
class Marker:
    """A durable marker's on-disk state and, when PRESENT, its payload.

    ``raw`` carries the exact on-disk bytes (PRESENT only), so the reconcile
    can compare BYTE-STRICTLY against :func:`canonical_marker_bytes` of the
    desired payload — a reorder, a type diff, an extra key or a whitespace diff
    all differ and are rewritten, while casa's own freshly-written marker (same
    canonical helper) is byte-identical and so never churns."""

    state: MarkerState
    payload: "dict | None" = None
    raw: "bytes | None" = None


class SpoolClosed(RuntimeError):
    """Raised by administrative operations on a closed spool. The request-path
    operations (:meth:`CallbackSpool.claim` / :meth:`publish_result`) return a
    neutral refusal instead — they must never raise into the HTTP handler."""


# ---------------------------------------------------------------------------
# names / keys
# ---------------------------------------------------------------------------


def canonical_marker_bytes(payload: dict) -> bytes:
    """The ONE canonical on-disk form of a durable marker's payload — the exact
    bytes the marker WRITER (:meth:`CallbackSpool._replace_json`, i.e.
    ``write_ready`` / ``write_index_entry``) emits, and the exact bytes the
    reconcile compares an on-disk marker against.

    Sorted keys + the most compact separators + no ASCII-escaping, UTF-8. Because
    writer and compare share this single helper, casa's own freshly-written
    marker is always byte-identical to ``canonical_marker_bytes(desired)`` — a
    steady-state pass finds it unchanged (no churn, no serialization-drift
    footgun) while ANY on-disk drift (a key reorder, a ``true``/``1.0`` type
    diff, an extra key, a whitespace diff) differs and is rewritten."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def state_hash(state: str) -> str:
    """The spool name for a consumer-minted ``state`` — sha256 hex. The hash,
    not the state, is what casa and the delivery nudge ever handle."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def index_key(artifact_realpath: str) -> str:
    """Discovery key for ``.index/`` — sha256 of the RESOLVED artifact root.

    A consumer computes ``sha256(realpath($CLAUDE_PLUGIN_ROOT))``, the one
    value it provably knows; it cannot know its casa registry name (bundled
    plugins are registered under scoped ``slug.manifest_name``). Resolving
    here as well as caller-side keeps the two ends symmetric when either is
    handed a symlinked path."""
    return hashlib.sha256(
        os.path.realpath(os.fspath(artifact_realpath)).encode("utf-8"),
    ).hexdigest()


def in_flight_key(plugin: str, state_hash_hex: str) -> str:
    """Key of the in-process in-flight set. Plugin-qualified: hashes are only
    unique within a plugin's own spool dir."""
    return f"{plugin}/{state_hash_hex}"


def spool_root() -> Path:
    """The configured spool root (``CASA_CALLBACK_SPOOL_ROOT`` overrides the
    ``/data`` default — the env key is exported to plugin subprocesses)."""
    return Path(os.environ.get(SPOOL_ROOT_ENV) or ROOT)


def _safe_component(name: str) -> bool:
    """True iff *name* is a single control-free path component (not . / ..)."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\0" in name:
        return False
    return not any(ord(c) < 0x20 for c in name)


def _is_hash(name: str) -> bool:
    return bool(_HASH_RE.match(name))


def _is_replace_temp(name: str) -> bool:
    """True for :meth:`CallbackSpool._replace_json` staging residue
    (``.ready.json.tmp-<pid>-<uuid>``, ``.<index-key>.json.tmp-…``) — a crash
    between staging and the rename leaves one, and nothing else would ever
    remove it."""
    return name.startswith(".") and REPLACE_TEMP_INFIX in name


def _hash_of_pending(name: str) -> str | None:
    if name.endswith(".json") and _is_hash(name[:-5]):
        return name[:-5]
    return None


# ---------------------------------------------------------------------------
# low-level fd helpers
# ---------------------------------------------------------------------------


def _fsync(fd: int, what: str) -> None:
    """Best-effort fsync following the ``atomic_io`` convention: at every call
    site the content is already committed and the caller's decision has been
    made, so misreporting a completed operation as failed would be strictly
    worse than the lost ordering guarantee (which only matters across a power
    crash). Repeated failure surfaces via these warnings."""
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.warning("callback-spool: fsync of %s failed: %s", what, exc)


def _open_dir(name: str, dir_fd: int) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _lstat_quiet(name: str, dir_fd: int):
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return None


#: Sentinel for :func:`_marker_lstat`: a metadata read that FAILED (any OSError
#: other than ENOENT), so the entry's presence is UNKNOWN — distinct from a
#: genuine absence (``None``). Never conflated with "gone".
_LSTAT_ERROR: object = object()


def _marker_lstat(name: str, dir_fd: int):
    """``lstat`` for the marker retirement/confirmation path, three-valued:

    * a ``stat_result`` — the entry is PRESENT;
    * ``None`` — the entry is genuinely ABSENT (``FileNotFoundError`` / ENOENT);
    * :data:`_LSTAT_ERROR` — a metadata read FAILED for any other reason
      (EACCES, EIO, ELOOP, ENOTDIR, …), so presence is UNKNOWN.

    Unlike :func:`_lstat_quiet` — which maps EVERY error to ``None`` and is
    therefore only safe where "absent-on-any-error" is a fail-SAFE best-effort
    peek — this never reads a non-ENOENT failure as absence. A retirement that
    cannot confirm removal must report FAILURE (so the reconciler surfaces
    ``callback_spool_error``), never a false success that leaves a surviving
    marker mistaken for gone."""
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return _LSTAT_ERROR


def _listdir_quiet(dir_fd: int) -> list[str]:
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
        # Never log the entry name: spool names are state hashes, and the log
        # surfaces are the ones INV-CB-006 keeps free of callback identifiers.
        logger.warning("callback-spool: unlink failed (errno %s)", exc.errno)
        return False


def _remove_entry(name: str, dir_fd: int, st) -> bool:
    """Remove any entry type. A directory under ``pending/`` or ``results/``
    is impossible in the protocol, so it is residue — ``rmtree`` (dir_fd is
    3.11+; the container base is 3.12) rather than a failing unlink."""
    try:
        if st is not None and stat.S_ISDIR(st.st_mode):
            shutil.rmtree(name, dir_fd=dir_fd)
            return True
    except OSError as exc:
        logger.warning("callback-spool: rmtree failed (errno %s)", exc.errno)
        return False
    return _unlink_quiet(name, dir_fd)


def _retire_marker_entry(name: str, dir_fd: int) -> bool:
    """Retire a durable-marker entry of ANY type (regular file, directory,
    symlink, FIFO, …) openat-relative to *dir_fd*.

    Returns True when the entry is now ABSENT — removed, or already gone — and
    False when a genuine removal FAILED and the entry SURVIVES. The caller must
    surface a False rather than treat it as absent (a stale/invalid marker that
    was NOT removed still blocks republication). Type-aware via ``_remove_entry``
    (``lstat`` + dir-vs-file), so a directory-shaped marker is ``rmtree``d rather
    than left behind by a raw ``unlink`` that assumes a regular file.

    Both the pre-removal type probe and the post-removal re-confirmation use
    :func:`_marker_lstat`, so ONLY a genuine ENOENT counts as absence: a
    metadata failure (EACCES/EIO/…) BEFORE removal is NOT read as "already
    gone" (it fails closed to False), and the same failure on the
    RE-CONFIRMATION is NOT read as "now gone" (a removal whose success cannot
    be confirmed is reported as failure, not a silent false success). A benign
    remove/vanish race still reports success (the confirmation sees ENOENT)."""
    st = _marker_lstat(name, dir_fd)
    if st is None:
        return True                          # genuinely absent (ENOENT)
    if st is _LSTAT_ERROR:
        return False                         # presence UNKNOWN — fail closed
    _remove_entry(name, dir_fd, st)
    return _marker_lstat(name, dir_fd) is None   # True only on confirmed ENOENT


def _write_new_file(name: str, dir_fd: int, data: bytes) -> None:
    """Create *name* exclusively (0600), write it whole, fsync it. Raises on
    any failure, leaving no partially-visible final name (the caller only ever
    publishes an already-fsynced inode by link)."""
    fd = os.open(name, _NEW_FILE_FLAGS, FILE_MODE, dir_fd=dir_fd)
    try:
        # The requested mode is only ever narrowed by the umask, never
        # widened — but pin it so the on-disk mode is deterministic.
        os.fchmod(fd, FILE_MODE)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        _fsync(fd, "staged file")
    finally:
        os.close(fd)


#: Sentinel for a token whose state could not be ESTABLISHED (an I/O failure
#: mid-probe). Distinct from ``None`` (positively absent or malformed) for the
#: same reason ``_LSTAT_ERROR`` is distinct from ENOENT: an unknowable state
#: is never grounds for the repair path to retire a possibly-valid token.
_TOKEN_ERROR = object()


def _classify_dir_token(dir_fd: int):
    """Three-state probe of the plugin dir's identity token (``.dir-id``):
    a valid token string; ``None`` when the entry is POSITIVELY absent or
    malformed (non-regular, wrong size, bad grammar — safe to repair); or
    :data:`_TOKEN_ERROR` when an ``open``/``fstat``/``read`` failure left its
    state unknowable (never safe to repair). A stat-pair compare alone cannot
    carry directory identity — ext4 recycles a freed inode number
    immediately, so a recreated directory can present the exact
    ``(st_dev, st_ino)`` of the one it replaced."""
    try:
        fd = os.open(DIR_ID_NAME, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None                          # positively absent
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None                      # positively a symlink — malformed
        return _TOKEN_ERROR
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != 32:
            return None                      # positively malformed
        chunks = bytearray()
        while len(chunks) < 33:
            piece = os.read(fd, 33 - len(chunks))
            if not piece:
                break
            chunks += piece
        # A regular 32-byte file with no legitimate concurrent writer must
        # read back exactly 32 bytes; anything else means the probe raced
        # something and the state is unknowable, not proven malformed.
        if len(chunks) != 32:
            return _TOKEN_ERROR
    except OSError:
        return _TOKEN_ERROR
    finally:
        os.close(fd)
    token = chunks.decode("ascii", errors="replace")
    return token if _DIR_ID_RE.match(token) else None


def _read_dir_token(dir_fd: int) -> str | None:
    """The GATE view of :func:`_classify_dir_token`: anything but a valid
    token — absent, malformed or unknowable alike — is ``None``, and the
    caller refuses. Only :meth:`CallbackSpool.ensure_plugin_dirs`'s repair
    path needs the three-state distinction."""
    token = _classify_dir_token(dir_fd)
    return token if isinstance(token, str) else None


def _read_marker_at(name: str, dir_fd: int) -> Marker:
    """Total, non-blocking, three-state marker read openat-relative to
    *dir_fd*.

    ABSENT is returned ONLY for a genuinely missing file (``ENOENT``). Every
    other obstruction is INVALID, never conflated with absent: a symlink
    (``O_NOFOLLOW`` ⇒ ``ELOOP``), a FIFO / socket / directory (rejected by the
    ``S_ISREG`` gate BEFORE any ``read`` — and the open is ``O_NONBLOCK``, so a
    FIFO cannot even block), an oversized body (bounded read), non-UTF-8, or a
    non-object JSON value. Mirrors the fail-closed FD discipline the rest of
    this module uses (``O_NOFOLLOW`` + ``fstat`` + ``S_ISREG``)."""
    try:
        fd = os.open(name, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return Marker(MarkerState.ABSENT)
    except OSError:
        return Marker(MarkerState.INVALID)   # symlink (ELOOP), ENXIO, perms, …
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return Marker(MarkerState.INVALID)
        if not stat.S_ISREG(st.st_mode):
            return Marker(MarkerState.INVALID)   # FIFO/socket/dir: no read
        chunks: list[bytes] = []
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
    except Exception:  # noqa: BLE001 — the reader is TOTAL: not just
        # ValueError/UnicodeDecodeError but e.g. a RecursionError from a
        # deeply-nested body that fits the size cap (60k opening brackets)
        # must decode to INVALID, never escape and violate the never-raises
        # contract the reconciler relies on.
        return Marker(MarkerState.INVALID)
    if not isinstance(obj, dict):
        return Marker(MarkerState.INVALID)
    # Expose the RAW on-disk bytes so the reconcile can compare byte-strictly
    # against canonical_marker_bytes(desired) — a parse-equal but not
    # byte-identical marker (reorder/type/whitespace/extra-key) is DIFFERING.
    return Marker(MarkerState.PRESENT, obj, raw=body)


def _link_once(src: str, src_dir_fd: int, dst: str, dst_dir_fd: int) -> bool:
    """Publish-once primitive: ``link(2)`` is the atomic no-replace rename the
    stdlib does not expose (there is no ``renameat2``/``RENAME_NOREPLACE``
    binding in CPython). ``EEXIST`` means a concurrent winner published first
    and is the arbiter of exactly-once; the caller never clobbers.

    ``follow_symlinks=False`` links the symlink itself rather than its target,
    so a swapped-in symlink cannot smuggle an outside inode into the spool —
    the subsequent ``S_ISREG`` gate then rejects it.

    Returns True when this caller published, False on ``EEXIST``; any other
    error propagates (the caller decides the fail-closed outcome).
    """
    try:
        os.link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                follow_symlinks=False)
        return True
    except FileExistsError:
        return False


# ---------------------------------------------------------------------------
# value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """Exclusive ownership of one consumed state. ``mtime`` is the MINT time
    (preserved through the claim link); ``dir_token`` pins the plugin spool
    directory this claim was taken from, so a removal + reinstall between
    claim and publish fails closed instead of depositing a credential into a
    different (recreated) directory. The token — a random ``.dir-id`` minted
    when the directory is created — is what carries that identity;
    ``dir_dev``/``dir_ino`` are kept as a second, cheaper gate, but cannot
    carry it alone (ext4 recycles a freed inode number immediately, so a
    recreated directory can present the exact stat pair of its
    predecessor)."""

    plugin: str
    state_hash: str
    mtime: float
    dir_dev: int
    dir_ino: int
    dir_token: str

    @property
    def key(self) -> str:
        return in_flight_key(self.plugin, self.state_hash)


@dataclass
class RecoveryReport:
    restored: list[tuple[str, str]] = field(default_factory=list)
    nudges: list[tuple[str, str]] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    temps_cleared: int = 0
    anomalies: list[str] = field(default_factory=list)


@dataclass
class SweepReport:
    deleted_pending: int = 0
    deleted_results: int = 0
    deleted_temps: int = 0
    deleted_collect: int = 0
    deleted_anomalous: int = 0
    deleted_capped: int = 0
    capped: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (self.deleted_pending + self.deleted_results
                + self.deleted_temps + self.deleted_collect
                + self.deleted_anomalous + self.deleted_capped)


# ---------------------------------------------------------------------------
# consumer-side reference helper
# ---------------------------------------------------------------------------


def mint(plugin_dir: Path | str, state: str) -> Path:
    """Mint a pending state — the CONSUMER's half of the contract, kept here
    as the executable reference (and the tests' minting primitive).

    ``pending/<hash>.json.part`` is written 0600 and fsynced, then published
    once by ``link(2)``; the final name existing is a hard error (state
    reuse), never an overwrite. Returns the published path.
    """
    h = state_hash(state)
    pending = Path(plugin_dir) / PENDING_DIR
    part, final = f"{h}.json{PART_SUFFIX}", f"{h}.json"
    dir_fd = os.open(pending, _DIR_FLAGS)
    try:
        _write_new_file(part, dir_fd, json.dumps({"v": 1}).encode("utf-8"))
        if not _link_once(part, dir_fd, final, dir_fd):
            _unlink_quiet(part, dir_fd)
            raise FileExistsError(
                errno.EEXIST, "state already minted", final)
        _fsync(dir_fd, str(pending))
        _unlink_quiet(part, dir_fd)
        _fsync(dir_fd, str(pending))
    finally:
        os.close(dir_fd)
    return pending / final


# ---------------------------------------------------------------------------
# the spool
# ---------------------------------------------------------------------------


class CallbackSpool:
    """One instance, owned by casa-core, pinned to the spool root.

    ``_lock`` serializes the dir-FD syscalls against :meth:`close` so a
    concurrent close (which nulls the root FD) can never interleave between
    the closed-check and a syscall — otherwise a ``dir_fd=None`` op would
    resolve against the process CWD (a fail-open). The request-path
    operations are a handful of syscalls each; the background passes (sweep,
    recovery) hold the lock for a whole scan, which is bounded by the
    per-plugin caps and runs off the event loop. The lock is NOT what makes
    consumption exactly-once — the real arbiter is ``link(2)``'s EEXIST,
    which holds across processes where no in-process lock can.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._closed = False
        self._in_flight: set[str] = set()
        os.makedirs(self.root, mode=DIR_MODE, exist_ok=True)
        self._root_fd = os.open(os.path.realpath(self.root), _DIR_FLAGS)
        try:
            os.fchmod(self._root_fd, DIR_MODE)
        except OSError as exc:                          # pragma: no cover
            logger.warning("callback-spool: chmod of root failed: %s", exc)

    # -- lifecycle ----------------------------------------------------------

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
            raise SpoolClosed("callback spool is closed")

    # -- directories --------------------------------------------------------

    def ensure_plugin_dirs(self, plugin: str) -> None:
        """Create ``<plugin>/{pending,results,.claims}`` at 0770. Idempotent —
        reconcile calls it on every pass. ``mkdir``'s mode is masked by the
        umask, so each directory's mode is pinned through its own O_NOFOLLOW
        FD afterwards."""
        # A leading dot is reserved for casa's own root-level structures
        # (``.index``): such a directory would be created here but skipped by
        # _plugin_dirs(), so it would never be swept, recovered or GC'd.
        if not _safe_component(plugin) or plugin.startswith("."):
            raise ValueError(f"unsafe plugin spool name {plugin!r}")
        with self._lock:
            self._require_open()
            self._mkdir(plugin, self._root_fd)
            pfd = _open_dir(plugin, self._root_fd)
            try:
                self._chmod_dir(pfd, plugin)
                for sub in (PENDING_DIR, RESULTS_DIR, CLAIMS_DIR):
                    self._mkdir(sub, pfd)
                    sfd = _open_dir(sub, pfd)
                    try:
                        self._chmod_dir(sfd, sub)
                    finally:
                        os.close(sfd)
                probe = _classify_dir_token(pfd)
                if probe is _TOKEN_ERROR:
                    # State unknowable (a transient I/O failure) — NEVER
                    # grounds to retire what may be a valid, live token that
                    # in-flight claims still carry. Abort; the next pass
                    # re-probes.
                    raise OSError(errno.EIO,
                                  f"{DIR_ID_NAME} state unknowable")
                if probe is None:
                    self._repair_dir_token(pfd)
                _fsync(pfd, plugin)
            finally:
                os.close(pfd)
            _fsync(self._root_fd, str(self.root))

    @staticmethod
    def _repair_dir_token(pfd: int) -> None:
        """Mint (or repair) the identity token, serialized cross-process by an
        exclusive ``flock`` on the plugin dir fd and RE-probed under it — a
        concurrent pass may have minted between the caller's probe and the
        lock, and a VALID token is never retired (exactly-once per directory
        life; in-flight claims carry it). A recreated directory always gets a
        FRESH token; the token, not the recyclable inode number, is what
        claim/discard/publish compare (see Claim.dir_token). A dir that
        existed before the token was introduced gets one on the first pass
        through here — claims taken before that mint fail closed, exactly
        like any other unprovable identity."""
        fcntl.flock(pfd, fcntl.LOCK_EX)
        try:
            probe = _classify_dir_token(pfd)
            if probe is _TOKEN_ERROR:
                raise OSError(errno.EIO,
                              f"{DIR_ID_NAME} state unknowable")
            if probe is None:
                if not _retire_marker_entry(DIR_ID_NAME, pfd):
                    raise OSError(errno.EIO,
                                  f"invalid {DIR_ID_NAME} survives retire")
                _write_new_file(DIR_ID_NAME, pfd,
                                uuid.uuid4().hex.encode("ascii"))
        finally:
            fcntl.flock(pfd, fcntl.LOCK_UN)

    @staticmethod
    def _mkdir(name: str, dir_fd: int) -> bool:
        """Create *name* if absent; True when this call created it (so the
        caller can fsync the parent exactly once, when it matters)."""
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
            logger.warning("callback-spool: chmod of %s failed: %s", what, exc)

    def _plugin_fd(self, plugin: str) -> int:
        """Open a per-plugin spool dir relative to the pinned root.

        The name guard lives HERE rather than at each entry point: this is the
        single funnel through which every plugin-scoped operation resolves a
        directory, so ``..`` (or any other non-component name) cannot escape
        the pinned root through a path an individual caller forgot to
        validate. Raises ``ValueError`` — never an ``OSError`` — so a caller's
        "directory is missing" branch can never absorb a traversal attempt.
        """
        if not _safe_component(plugin):
            raise ValueError(f"unsafe plugin spool name {plugin!r}")
        return _open_dir(plugin, self._root_fd)

    def _plugin_dirs(self) -> list[str]:
        """Names of the per-plugin spool dirs (``.index`` and any stray
        non-directory entry excluded)."""
        out = []
        for name in _listdir_quiet(self._root_fd):
            if name == INDEX_DIR or not _safe_component(name):
                continue
            st = _lstat_quiet(name, self._root_fd)
            if st is not None and stat.S_ISDIR(st.st_mode):
                out.append(name)
        return sorted(out)

    # -- readiness marker + discovery index ---------------------------------

    def write_ready(self, plugin: str, payload: dict) -> None:
        """Publish ``<plugin>/ready.json`` — the POSITIVE readiness marker,
        written only AFTER the routing overlay swap so it can never be falsely
        positive. Replacing (not publish-once): reconcile rebuilds the marker
        on every pass, and the marker is advisory — the overlay alone decides
        what the endpoint serves, so a stale marker cannot open a route."""
        with self._lock:
            self._require_open()
            pfd = self._plugin_fd(plugin)
            try:
                self._replace_json(READY_NAME, pfd, payload, plugin)
            finally:
                os.close(pfd)

    def delete_ready(self, plugin: str) -> bool:
        """Retire the marker (of ANY type) and fsync its directory — done BEFORE
        the unrouting overlay swap, so a crash mid-unroute can only leave the
        route closed with the marker already gone (never the reverse).

        Returns True when the marker is now absent (removed, or already gone),
        False when a genuine removal FAILED and the entry survives — surfaced by
        the reconciler rather than mistaken for both-absent. Type-aware: a
        directory/FIFO/symlink-shaped ``ready.json`` is removed (a raw unlink
        would fail on a directory and leave it to block republication)."""
        with self._lock:
            self._require_open()
            try:
                pfd = self._plugin_fd(plugin)
            except FileNotFoundError:
                return True                  # dir already gone — marker absent
            except OSError:
                return False                 # dir unreachable — not confirmed gone
            try:
                gone = _retire_marker_entry(READY_NAME, pfd)
                _fsync(pfd, plugin)
                return gone
            finally:
                os.close(pfd)

    def read_marker(self, plugin: str) -> Marker:
        """Three-state read of ``<plugin>/ready.json`` — the reconciler's
        durable on-disk truth.

        Returns ABSENT (no marker), INVALID (present but non-regular /
        unreadable / oversized / malformed), or PRESENT with the payload.
        Distinguishing INVALID from ABSENT is what lets a stale-but-unreadable
        (e.g. oversized, or a swapped-in FIFO) marker be republished rather than
        conflated with absent and left to survive a failed rewrite. Never
        blocks and never raises (see :func:`_read_marker_at`)."""
        with self._lock:
            if self._closed or not _safe_component(plugin):
                return Marker(MarkerState.ABSENT)
            try:
                pfd = self._plugin_fd(plugin)
            except FileNotFoundError:
                return Marker(MarkerState.ABSENT)   # no plugin dir = no marker
            except (OSError, ValueError):
                return Marker(MarkerState.INVALID)
            try:
                return _read_marker_at(READY_NAME, pfd)
            finally:
                os.close(pfd)

    def read_index_marker(self, artifact_realpath: str) -> Marker:
        """Three-state read of ``.index/<key>.json`` for an artifact path —
        the durable-marker companion of :meth:`read_marker` (same semantics)."""
        with self._lock:
            if self._closed:
                return Marker(MarkerState.ABSENT)
            try:
                ifd = _open_dir(INDEX_DIR, self._root_fd)
            except FileNotFoundError:
                return Marker(MarkerState.ABSENT)   # no index dir = no entry
            except OSError:
                return Marker(MarkerState.INVALID)
            try:
                return _read_marker_at(
                    f"{index_key(artifact_realpath)}.json", ifd)
            finally:
                os.close(ifd)

    def write_index_entry(self, artifact_realpath: str, payload: dict) -> None:
        """Publish ``.index/<sha256(realpath(artifact_root))>.json`` — how a
        consumer finds its spool dir without knowing its registry name."""
        with self._lock:
            self._require_open()
            ifd = self._index_fd(create=True)
            try:
                self._replace_json(f"{index_key(artifact_realpath)}.json",
                                   ifd, payload, INDEX_DIR)
            finally:
                os.close(ifd)

    def delete_index_entry(self, artifact_realpath: str) -> bool:
        """Retire the ``.index`` entry (of ANY type) for *artifact_realpath*.
        Returns True when it is now absent, False when a removal genuinely
        failed (see :meth:`delete_ready`)."""
        with self._lock:
            self._require_open()
            try:
                ifd = self._index_fd(create=False)
            except FileNotFoundError:
                return True                  # no index dir — entry absent
            except OSError:
                return False
            try:
                gone = _retire_marker_entry(
                    f"{index_key(artifact_realpath)}.json", ifd)
                _fsync(ifd, INDEX_DIR)
                return gone
            finally:
                os.close(ifd)

    def published_plugins(self) -> list[str]:
        """Plugin dirs that currently carry a ``ready.json`` marker of ANY type —
        the DURABLE readiness inventory the reconciler reconciles against the
        desired routed set. Reading on-disk truth (not the in-memory previous
        overlay, which is empty across a restart) is what lets a marker for a
        plugin no longer routed be retired after a reboot.

        A non-regular ``ready.json`` (a swapped-in directory/FIFO/symlink) is
        INCLUDED, not silently omitted: it is an INVALID orphan that must still
        be enumerated so a trustworthy pass retires it — omitting it would leave
        an invalid marker to survive forever and block republication."""
        with self._lock:
            if self._closed:
                return []
            out: list[str] = []
            for plugin in self._plugin_dirs():
                try:
                    pfd = self._plugin_fd(plugin)
                except (OSError, ValueError):
                    continue
                try:
                    if _lstat_quiet(READY_NAME, pfd) is not None:
                        out.append(plugin)
                finally:
                    os.close(pfd)
            return out

    def index_keys(self) -> list[str]:
        """Discovery-index keys currently published under ``.index/`` — the
        ``<sha256>.json`` entries (staging residue excluded), returned WITHOUT
        the ``.json`` suffix. Enumeration is by NAME, so a non-regular entry (a
        swapped-in directory/FIFO/symlink named ``<sha256>.json``) is included
        too — an INVALID orphan the reconciler must still see to retire. The
        reconciler retires any key the desired routed set no longer covers;
        :meth:`delete_index_key` retires one by key."""
        with self._lock:
            if self._closed:
                return []
            try:
                ifd = _open_dir(INDEX_DIR, self._root_fd)
            except OSError:
                return []
            try:
                out: list[str] = []
                for name in _listdir_quiet(ifd):
                    if _is_replace_temp(name):
                        continue
                    if name.endswith(".json") and _is_hash(name[:-5]):
                        out.append(name[:-5])
                return sorted(out)
            finally:
                os.close(ifd)

    def delete_index_key(self, key: str) -> bool:
        """Retire one discovery-index entry (of ANY type) by its already-computed
        KEY. Returns True when the entry is now absent, False when a removal
        genuinely failed (see :meth:`delete_ready`).

        The durable-inventory reconcile knows the on-disk key (a sha256 hex),
        not the artifact path it was derived from, so it cannot go through
        :meth:`delete_index_entry` (which re-hashes a path). Guarded like every
        index op — a non-hash key raises ``ValueError`` rather than resolving
        outside ``.index/`` — and a closed spool raises ``SpoolClosed``."""
        if not _is_hash(key):
            raise ValueError(f"unsafe index key {key!r}")
        with self._lock:
            self._require_open()
            try:
                ifd = self._index_fd(create=False)
            except FileNotFoundError:
                return True                  # no index dir — entry absent
            except OSError:
                return False
            try:
                gone = _retire_marker_entry(f"{key}.json", ifd)
                _fsync(ifd, INDEX_DIR)
                return gone
            finally:
                os.close(ifd)

    def _index_fd(self, *, create: bool) -> int:
        if create and self._mkdir(INDEX_DIR, self._root_fd):
            # The directory entry itself must be durable before an entry
            # inside it is: otherwise a power crash can keep the index entry's
            # inode while losing the directory that names it.
            _fsync(self._root_fd, str(self.root))
        fd = _open_dir(INDEX_DIR, self._root_fd)
        if create:
            self._chmod_dir(fd, INDEX_DIR)
        return fd

    def _replace_json(self, name: str, dir_fd: int, payload: dict,
                      what: str) -> None:
        """Atomic replacing publish for the two ADVISORY files (ready.json and
        an index entry): staged 0600 + fsync, then renamed over the target,
        then a directory fsync. Never used for pending/claims/results — those
        are publish-once by ``link(2)``. Serialized through the SHARED
        :func:`canonical_marker_bytes`, so a marker casa writes is byte-identical
        to what the reconcile's compare treats as unchanged — no steady-state
        churn."""
        data = canonical_marker_bytes(payload)
        tmp = f".{name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        _write_new_file(tmp, dir_fd, data)
        try:
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            _unlink_quiet(tmp, dir_fd)
            raise
        _fsync(dir_fd, what)

    # -- in-flight set ------------------------------------------------------

    def in_flight(self) -> set[str]:
        """Plugin-qualified hashes a handler is currently processing. The
        periodic recovery pass skips these: handler and recovery run in the
        SAME process, so this set is what makes "a live claim is never
        restored under a working handler" true. Boot passes have no live
        handlers by construction and ignore it."""
        with self._lock:
            return set(self._in_flight)

    # -- claim ----------------------------------------------------------

    def claim(self, plugin: str, state_hash_hex: str, *, now: float) -> Claim | None:
        """Consume ``pending/<hash>.json`` exactly once.

        Every refusal returns ``None`` — replay, expired, never-minted and
        unknown-plugin all lose identically, because the caller renders one
        neutral response for all of them (INV-CB-005) and a differentiated
        outcome would be an enumeration oracle.

        Sequence: ``link`` into ``.claims/<hash>`` (EEXIST/ENOENT ⇒ lose) →
        fsync ``.claims/`` → unlink the pending name → fsync ``pending/``.
        Linking BEFORE unlinking is what makes a crash recoverable: the worst
        residue is a claim plus its pending twin, which the recovery pass
        converges. The TTL/skew gates then run on the claim's own mtime —
        which ``link`` preserved, so it is still the MINT time.
        """
        if not _safe_component(plugin) or not _is_hash(state_hash_hex):
            return None
        with self._lock:
            if self._closed:
                return None                  # fail closed; never raise into HTTP
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return None                  # unrouted / removed plugin
            try:
                dir_st = os.fstat(pfd)
                dir_token = _read_dir_token(pfd)
                if dir_token is None:
                    return None      # identity unprovable — refuse
                try:
                    pend = _open_dir(PENDING_DIR, pfd)
                except OSError:
                    return None
                try:
                    claims = _open_dir(CLAIMS_DIR, pfd)
                except OSError:
                    os.close(pend)
                    return None
                try:
                    return self._claim_locked(plugin, state_hash_hex, now,
                                              pend, claims, dir_st, dir_token)
                finally:
                    os.close(pend)
                    os.close(claims)
            finally:
                os.close(pfd)

    def _claim_locked(self, plugin: str, h: str, now: float, pend: int,
                      claims: int, dir_st, dir_token: str) -> Claim | None:
        src = f"{h}.json"
        try:
            if not _link_once(src, pend, h, claims):
                return None      # a claim already exists — replay loses
        except OSError:
            return None          # never minted, vanished, or an FS fault
        _fsync(claims, CLAIMS_DIR)
        _unlink_quiet(src, pend)
        _fsync(pend, PENDING_DIR)

        st = _lstat_quiet(h, claims)
        if st is None or not stat.S_ISREG(st.st_mode):
            # A non-regular inode can only have come from a symlinked pending
            # name (linked as the symlink itself, never followed) — refuse it.
            _remove_entry(h, claims, st)
            _fsync(claims, CLAIMS_DIR)
            return None
        if st.st_mtime > now + SKEW_S:
            logger.info("callback-spool: dropping future-mtime claim (%s)", plugin)
            _unlink_quiet(h, claims)
            _fsync(claims, CLAIMS_DIR)
            return None
        if now - st.st_mtime > PENDING_TTL_S:
            _unlink_quiet(h, claims)
            _fsync(claims, CLAIMS_DIR)
            return None
        claim = Claim(plugin=plugin, state_hash=h, mtime=st.st_mtime,
                      dir_dev=dir_st.st_dev, dir_ino=dir_st.st_ino,
                      dir_token=dir_token)
        self._in_flight.add(claim.key)
        return claim

    def discard_claim(self, claim: Claim) -> None:
        """Drop a claim without publishing (the handler's refusal paths).
        The state stays consumed — that is the point of claim-by-rename."""
        with self._lock:
            self._in_flight.discard(claim.key)
            if self._closed:
                return
            try:
                pfd = self._plugin_fd(claim.plugin)
            except (OSError, ValueError):
                return
            try:
                # Same identity gate as the publish path: after a removal +
                # reinstall this name denotes a different directory, and the
                # claim being dropped is not ours to delete there. The token
                # is what proves identity; the stat pair is a second gate
                # only (an inode number is recycled, a token never).
                st = os.fstat(pfd)
                if (st.st_dev, st.st_ino) != (claim.dir_dev, claim.dir_ino):
                    return
                if _read_dir_token(pfd) != claim.dir_token:
                    return
                try:
                    claims = _open_dir(CLAIMS_DIR, pfd)
                except OSError:
                    return
                try:
                    _unlink_quiet(claim.state_hash, claims)
                    _fsync(claims, CLAIMS_DIR)
                finally:
                    os.close(claims)
            finally:
                os.close(pfd)

    # -- publish --------------------------------------------------------

    def publish_result(self, claim: Claim, record: dict) -> bool:
        """Publish the result for *claim*, never partially visible.

        Exact sequence: write + fsync a private ``.claims/.tmp-<hash>`` →
        ``link`` it into ``results/<hash>.json`` → fsync ``results/`` →
        unlink the temp → fsync ``.claims/`` → unlink the claim → fsync
        ``.claims/``.

        The temp is unlinked before the claim so a credential-bearing inode
        never keeps a second hard link past publication; a crash in that
        window leaves a temp whose hash already has a result, which recovery
        clears. ``EEXIST`` on the link is a hard anomaly (a result already
        exists for this state): the claim and temp are still cleaned up and
        the caller renders the same neutral response.

        On a *staging* failure this method returns ``False`` and deliberately
        LEAVES the claim, so the recovery pass can restore the flow to
        ``pending/`` rather than a transient write eating it (see
        :meth:`_publish_locked`). Note the sole production caller —
        ``callback_http`` — does not rely on that: on any ``False``/raise it
        calls :meth:`discard_claim`, so the restore-to-pending branch is
        reachable only from a crash-recovery path, never from a live handler's
        write failure (the endpoint is unauthenticated, so a consumed state
        stays consumed rather than reopening a replay window).
        """
        with self._lock:
            try:
                try:
                    payload = json.dumps(record).encode("utf-8")
                except (TypeError, ValueError):
                    logger.warning(
                        "callback-spool: result record is not serializable")
                    return False
                return self._publish_guarded(claim, payload)
            finally:
                # Cleared no matter how this exits: a hash left in the set
                # would make the periodic recovery pass skip its claim for the
                # rest of the process's life.
                self._in_flight.discard(claim.key)

    def _publish_guarded(self, claim: Claim, payload: bytes) -> bool:
        """Open the claim's directories (fail-closed on identity drift) and
        run the publish sequence. Called with ``_lock`` held."""
        if self._closed:
            return False
        try:
            pfd = self._plugin_fd(claim.plugin)
        except (OSError, ValueError):
            logger.warning("callback-spool: plugin dir vanished before publish")
            return False
        try:
            st = os.fstat(pfd)
            if ((st.st_dev, st.st_ino) != (claim.dir_dev, claim.dir_ino)
                    or _read_dir_token(pfd) != claim.dir_token):
                # Removed + recreated between claim and publish: this is a
                # different directory (fresh token — the stat pair alone can
                # recur, since ext4 recycles freed inode numbers). Fail
                # closed rather than deposit a credential into a re-installed
                # plugin's spool.
                logger.warning("callback-spool: plugin dir replaced mid-flow; "
                               "refusing to publish")
                return False
            try:
                claims = _open_dir(CLAIMS_DIR, pfd)
            except OSError:
                return False
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(claims)
                return False
            try:
                return self._publish_locked(claim, payload, claims, results)
            finally:
                os.close(claims)
                os.close(results)
        finally:
            os.close(pfd)

    def _publish_locked(self, claim: Claim, payload: bytes, claims: int,
                        results: int) -> bool:
        h = claim.state_hash
        tmp, final = f"{TEMP_PREFIX}{h}", f"{h}.json"
        # A pre-existing temp is residue from a crashed attempt at THIS hash:
        # the in-flight set excludes a live writer, so reclaiming the
        # deterministic name is safe (and recovery clears it the other way).
        _unlink_quiet(tmp, claims)
        try:
            _write_new_file(tmp, claims, payload)
        except OSError as exc:
            logger.warning("callback-spool: result staging failed (errno %s)",
                           exc.errno)
            _unlink_quiet(tmp, claims)
            # The claim is deliberately LEFT: recovery restores it to pending
            # so the flow is not silently eaten by a transient write failure.
            return False
        try:
            published = _link_once(tmp, claims, final, results)
        except OSError as exc:
            # A genuine FS fault (not EEXIST): drop only the temp and LEAVE
            # the claim, so the recovery pass restores the flow to pending
            # instead of the transient failure silently eating it.
            logger.warning("callback-spool: result publish failed (errno %s)",
                           exc.errno)
            _unlink_quiet(tmp, claims)
            _fsync(claims, CLAIMS_DIR)
            return False
        if published:
            _fsync(results, RESULTS_DIR)
        else:
            logger.warning(
                "callback-spool: result already exists for a claimed state "
                "(plugin=%s) — anomaly, dropping", claim.plugin)
        _unlink_quiet(tmp, claims)
        _fsync(claims, CLAIMS_DIR)
        _unlink_quiet(h, claims)
        _fsync(claims, CLAIMS_DIR)
        return published

    # -- read side (delivery nudge / consumers) -----------------------------

    def has_result(self, plugin: str, state_hash_hex: str) -> bool:
        with self._lock:
            if self._closed or not _safe_component(plugin) \
                    or not _is_hash(state_hash_hex):
                return False
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return False
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(pfd)
                return False
            try:
                st = _lstat_quiet(f"{state_hash_hex}.json", results)
                return st is not None and stat.S_ISREG(st.st_mode)
            finally:
                os.close(results)
                os.close(pfd)

    def list_results(self, plugin: str) -> list[str]:
        """Published result hashes for *plugin* (the recovery invariant's
        input: any result lacking a settled episode is re-enqueued)."""
        with self._lock:
            if self._closed or not _safe_component(plugin):
                return []
            try:
                pfd = self._plugin_fd(plugin)
            except (OSError, ValueError):
                return []
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(pfd)
                return []
            try:
                return sorted(
                    h for h in (_hash_of_pending(n)
                                for n in _listdir_quiet(results))
                    if h is not None)
            finally:
                os.close(results)
                os.close(pfd)

    def plugins(self) -> list[str]:
        with self._lock:
            if self._closed:
                return []
            return self._plugin_dirs()

    # -- recovery -------------------------------------------------------

    def recovery_pass(self, *, now: float, boot: bool) -> RecoveryReport:
        """Converge ``.claims/`` residue left by a crash.

        Two phases per plugin, in this order and for this reason: orphan
        ``.tmp-<hash>`` temps are unlinked and made durable FIRST, so that
        when the same hash's claim is restored to ``pending/`` the retry does
        not find its deterministic temp name occupied.

        Then per claim: stale or future-mtime ⇒ delete; a matching published
        result ⇒ report for the delivery nudge and remove the claim (never
        re-mint a completed flow); otherwise restore it to ``pending/`` by
        publish-once link, keeping its mint mtime, so the crash window
        between claim and result write does not silently eat a flow.

        A periodic pass additionally skips in-flight hashes and claims younger
        than :data:`RESTORE_GRACE_S`; a boot pass has no live handlers by
        construction and skips neither.
        """
        report = RecoveryReport()
        with self._lock:
            if self._closed:
                return report
            for plugin in self._plugin_dirs():
                try:
                    pfd = self._plugin_fd(plugin)
                except (OSError, ValueError):
                    continue
                try:
                    self._recover_plugin(plugin, pfd, now, boot, report)
                finally:
                    os.close(pfd)
        return report

    def _recover_plugin(self, plugin: str, pfd: int, now: float, boot: bool,
                        report: RecoveryReport) -> None:
        try:
            claims = _open_dir(CLAIMS_DIR, pfd)
        except OSError:
            return
        try:
            try:
                pend = _open_dir(PENDING_DIR, pfd)
            except OSError:
                return
            try:
                results = _open_dir(RESULTS_DIR, pfd)
            except OSError:
                os.close(pend)               # no leak on the second open
                return
            try:
                names = _listdir_quiet(claims)
                self._recover_temps(plugin, claims, names, boot, report)
                for name in names:
                    if name.startswith(TEMP_PREFIX):
                        continue             # handled in the temp phase
                    self._recover_claim(plugin, name, claims, pend, results,
                                        now, boot, report)
            finally:
                os.close(pend)
                os.close(results)
        finally:
            os.close(claims)

    def _recover_temps(self, plugin: str, claims: int, names: list[str],
                       boot: bool, report: RecoveryReport) -> None:
        for name in names:
            if not name.startswith(TEMP_PREFIX):
                continue
            h = name[len(TEMP_PREFIX):]
            if not boot and _is_hash(h) \
                    and in_flight_key(plugin, h) in self._in_flight:
                continue                     # a live writer owns this temp
            if _unlink_quiet(name, claims):
                _fsync(claims, CLAIMS_DIR)
                report.temps_cleared += 1

    def _recover_claim(self, plugin: str, name: str, claims: int, pend: int,
                       results: int, now: float, boot: bool,
                       report: RecoveryReport) -> None:
        if not _is_hash(name):
            st = _lstat_quiet(name, claims)
            _remove_entry(name, claims, st)
            _fsync(claims, CLAIMS_DIR)
            report.anomalies.append(f"{plugin}: unparseable claim entry")
            return
        if not boot and in_flight_key(plugin, name) in self._in_flight:
            return
        st = _lstat_quiet(name, claims)
        if st is None:
            return
        # A published result outranks EVERY age gate and every type anomaly on
        # the claim side: the flow completed, only its delivery nudge may be
        # missing, and the result's own TTL is the thing that bounds it. The
        # claim's mtime is the MINT time, so a slow authorization (minted 31
        # minutes ago, result written seconds before the crash) would otherwise
        # be "stale" here and its nudge silently dropped while the credential
        # sits live in results/.
        res = _lstat_quiet(f"{name}.json", results)
        if res is not None and stat.S_ISREG(res.st_mode):
            report.nudges.append((plugin, name))
            _remove_entry(name, claims, st)
            _fsync(claims, CLAIMS_DIR)
            return
        if not stat.S_ISREG(st.st_mode):
            _remove_entry(name, claims, st)
            _fsync(claims, CLAIMS_DIR)
            report.anomalies.append(f"{plugin}: non-regular claim entry")
            return
        if st.st_mtime > now + SKEW_S or now - st.st_mtime > PENDING_TTL_S:
            _unlink_quiet(name, claims)
            _fsync(claims, CLAIMS_DIR)
            report.dropped.append((plugin, name))
            return
        if not boot and now - st.st_mtime < RESTORE_GRACE_S:
            return
        try:
            restored = _link_once(name, claims, f"{name}.json", pend)
        except OSError as exc:
            # ``exc`` itself is not logged: an OSError from ``linkat`` carries
            # the operand names, i.e. the state hash (INV-CB-006 hygiene).
            logger.warning("callback-spool: claim restore failed (errno %s)",
                           exc.errno)
            return
        if restored:
            _fsync(pend, PENDING_DIR)
            report.restored.append((plugin, name))
        # EEXIST: a pending twin already exists (a crash between the claim's
        # link and the source unlink) — the claim is simply superseded.
        _unlink_quiet(name, claims)
        _fsync(claims, CLAIMS_DIR)

    # -- sweep ----------------------------------------------------------

    def sweep(self, *, now: float) -> SweepReport:
        """TTL + future-mtime deletion across the five name classes, then the
        per-plugin caps. Bare ``.claims/<hash>`` entries are deliberately NOT
        swept: they belong to the recovery pass, and deleting a young claim
        here would silently eat an in-flight authorization."""
        report = SweepReport()
        with self._lock:
            if self._closed:
                return report
            for plugin in self._plugin_dirs():
                try:
                    pfd = self._plugin_fd(plugin)
                except (OSError, ValueError):
                    continue
                try:
                    self._sweep_plugin(plugin, pfd, now, report)
                finally:
                    os.close(pfd)
            self._sweep_index(now, report)
        return report

    def _sweep_index(self, now: float, report: SweepReport) -> None:
        """`.index/` holds reconcile-owned entries plus, after a crash, this
        module's staging residue — only the latter is sweep-owned."""
        try:
            ifd = _open_dir(INDEX_DIR, self._root_fd)
        except OSError:
            return
        try:
            if self._sweep_replace_temps(ifd, now, report):
                _fsync(ifd, INDEX_DIR)
        finally:
            os.close(ifd)

    def _sweep_plugin(self, plugin: str, pfd: int, now: float,
                      report: SweepReport) -> None:
        # The plugin dir's own level: ready.json staging residue only. Nothing
        # else there is sweep-owned (ready.json is reconcile's, the three
        # subdirs are handled below).
        if self._sweep_replace_temps(pfd, now, report):
            _fsync(pfd, plugin)
        for sub, handler in ((PENDING_DIR, self._sweep_pending),
                             (RESULTS_DIR, self._sweep_results),
                             (CLAIMS_DIR, self._sweep_claims)):
            try:
                fd = _open_dir(sub, pfd)
            except OSError:
                continue
            try:
                handler(plugin, fd, now, report)
                _fsync(fd, sub)
            finally:
                os.close(fd)

    def _sweep_replace_temps(self, fd: int, now: float,
                             report: SweepReport) -> bool:
        """Age-sweep `_replace_json` staging residue in *fd*. Returns True if
        anything was removed (so the caller fsyncs)."""
        removed = False
        for name in _listdir_quiet(fd):
            if not _is_replace_temp(name):
                continue
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            if not stat.S_ISREG(st.st_mode) or self._expired(st, now, TEMP_TTL_S):
                if _remove_entry(name, fd, st):
                    report.deleted_temps += 1
                    removed = True
        return removed

    def _expired(self, st, now: float, ttl: float) -> bool:
        # Future beyond the skew allowance is fail-closed: a forward clock
        # jump must not park entries that regain validity when it returns.
        return st.st_mtime > now + SKEW_S or now - st.st_mtime > ttl

    def _sweep_pending(self, plugin: str, fd: int, now: float,
                       report: SweepReport) -> None:
        live: list[tuple[float, str]] = []
        for name in _listdir_quiet(fd):
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            is_part = name.endswith(PART_SUFFIX)
            h = _hash_of_pending(name[:-len(PART_SUFFIX)] if is_part else name)
            if h is None or not stat.S_ISREG(st.st_mode):
                _remove_entry(name, fd, st)
                report.deleted_anomalous += 1
                report.anomalies.append(f"{plugin}/pending: {name!r}")
                continue
            if self._expired(st, now, TEMP_TTL_S if is_part else PENDING_TTL_S):
                if _unlink_quiet(name, fd):
                    if is_part:
                        report.deleted_temps += 1
                    else:
                        report.deleted_pending += 1
                continue
            # `.part` files count toward the cap: they occupy the same
            # directory and a consumer looping on a failing publish would
            # otherwise fill /data with staging files the cap never sees.
            live.append((st.st_mtime, name))
        self._apply_cap(plugin, PENDING_DIR, fd, live, MAX_PENDING, report)

    def _sweep_results(self, plugin: str, fd: int, now: float,
                       report: SweepReport) -> None:
        live: list[tuple[float, str]] = []
        for name in _listdir_quiet(fd):
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            is_collect = name.startswith(COLLECT_PREFIX)
            h = _hash_of_pending(name)
            if (h is None and not is_collect) or not stat.S_ISREG(st.st_mode):
                _remove_entry(name, fd, st)
                report.deleted_anomalous += 1
                report.anomalies.append(f"{plugin}/results: {name!r}")
                continue
            if self._expired(st, now, RESULT_TTL_S):
                if _unlink_quiet(name, fd):
                    if is_collect:
                        report.deleted_collect += 1
                    else:
                        report.deleted_results += 1
                continue
            if not is_collect:
                live.append((st.st_mtime, name))
        # Consumer-held `.collect-*` entries are excluded from the cap: they
        # are already claimed and about to be read.
        self._apply_cap(plugin, RESULTS_DIR, fd, live, MAX_RESULTS, report)

    def _sweep_claims(self, plugin: str, fd: int, now: float,
                      report: SweepReport) -> None:
        for name in _listdir_quiet(fd):
            if not name.startswith(TEMP_PREFIX):
                continue                     # bare claims belong to recovery
            h = name[len(TEMP_PREFIX):]
            if _is_hash(h) and in_flight_key(plugin, h) in self._in_flight:
                continue                     # a live writer owns this temp
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            if not stat.S_ISREG(st.st_mode) or self._expired(st, now, TEMP_TTL_S):
                if _remove_entry(name, fd, st):
                    report.deleted_temps += 1

    def _apply_cap(self, plugin: str, sub: str, fd: int,
                   live: list[tuple[float, str]], cap: int,
                   report: SweepReport) -> None:
        if len(live) <= cap:
            return
        live.sort()                          # oldest mtime first
        for _mtime, name in live[:len(live) - cap]:
            if _unlink_quiet(name, fd):
                report.deleted_capped += 1
        report.capped.append(f"{plugin}/{sub}")
        logger.warning("callback-spool: %s/%s exceeded %d entries — "
                       "oldest-first deletion applied", plugin, sub, cap)

    # -- gated orphan-dir GC ----------------------------------------------

    def gc_orphan_dirs(self, *, registry_valid: bool,
                       member_plugins: set[str], now: float) -> list[str]:
        """Remove spool dirs of plugins that are no longer installed.

        **Gated, not fail-destructive**: with anything other than a valid
        registry load this is a NO-OP, because a membership set derived from a
        failed load would vaporize every plugin's in-flight authorizations.
        Membership keys on registry ENTRIES (not resolution success — an
        artifact checksum hiccup must not delete a live spool), and a dir is
        removed only when it has been quiescent for :data:`QUIESCENCE_S` AND
        holds no entry younger than the pending TTL.
        """
        if registry_valid is not True:
            return []
        removed: list[str] = []
        with self._lock:
            if self._closed:
                return []
            for plugin in self._plugin_dirs():
                if plugin in member_plugins:
                    continue
                newest = self._newest_mtime(plugin)
                if newest is None:
                    continue
                age = now - newest
                # Both spec conditions, kept independent on purpose: today
                # QUIESCENCE_S subsumes the pending-TTL floor, but a future
                # TTL change must not be able to invert that relationship.
                if age < QUIESCENCE_S or age < PENDING_TTL_S:
                    continue
                try:
                    shutil.rmtree(plugin, dir_fd=self._root_fd)
                except OSError as exc:
                    logger.warning("callback-spool: orphan GC of %r failed: %s",
                                   plugin, exc)
                    continue
                _fsync(self._root_fd, str(self.root))
                removed.append(plugin)
                logger.info("callback-spool: removed orphan spool dir %r", plugin)
        return removed

    def remove_plugin(self, plugin: str) -> bool:
        """Recursively delete a plugin's ENTIRE spool dir (removal lifecycle).
        The explicit, immediate counterpart of the age-gated
        :meth:`gc_orphan_dirs`: when the operator removes a plugin, its
        in-flight authorizations are gone with it, so there is nothing to
        preserve and no quiescence window to honour.

        Guarded exactly like every dir op — an unsafe / dotted name raises
        ``ValueError`` (never resolves against the CWD), a closed spool is a
        no-op — and the root is fsynced afterwards so the unlink is durable.
        ``ready.json`` lives INSIDE the plugin dir, so this retires it too;
        the ``.index`` discovery entry keyed by the plugin's ARTIFACT PATH
        (not derivable from the plugin name) is retired separately by the
        reconcile's durable marker pass. Returns True when a dir was removed.
        """
        if not _safe_component(plugin) or plugin.startswith("."):
            raise ValueError(f"unsafe plugin spool name {plugin!r}")
        with self._lock:
            if self._closed:
                return False
            try:
                shutil.rmtree(plugin, dir_fd=self._root_fd)
            except FileNotFoundError:
                return False
            except OSError as exc:
                logger.warning("callback-spool: remove_plugin %r failed: %s",
                               plugin, exc)
                return False
            _fsync(self._root_fd, str(self.root))
            logger.info("callback-spool: removed spool dir %r (plugin removed)",
                        plugin)
            return True

    def _newest_mtime(self, plugin: str) -> float | None:
        """Newest mtime anywhere in the plugin's spool tree, including the
        directories themselves (a directory's mtime moves whenever an entry is
        added or removed, which is exactly the quiescence signal)."""
        try:
            pfd = self._plugin_fd(plugin)
        except (OSError, ValueError):
            return None
        try:
            return self._newest_in(pfd, depth=3)
        finally:
            os.close(pfd)

    def _newest_in(self, fd: int, depth: int) -> float:
        try:
            newest = os.fstat(fd).st_mtime
        except OSError:                                  # pragma: no cover
            return 0.0
        for name in _listdir_quiet(fd):
            st = _lstat_quiet(name, fd)
            if st is None:
                continue
            newest = max(newest, st.st_mtime)
            if depth > 0 and stat.S_ISDIR(st.st_mode):
                try:
                    sub = _open_dir(name, fd)
                except OSError:
                    continue
                try:
                    newest = max(newest, self._newest_in(sub, depth - 1))
                finally:
                    os.close(sub)
        return newest


# ---------------------------------------------------------------------------
# Module singleton (initialised once at boot by casa_core, like the outbox).
# ---------------------------------------------------------------------------

_SPOOL: "CallbackSpool | None" = None


def init_spool(root: "Path | str | None" = None) -> "CallbackSpool":
    """Create (or replace) the process-wide spool. Boot wiring only — the
    reconciler, the HTTP handler and the sweeper all read :func:`get_spool`."""
    global _SPOOL
    _SPOOL = CallbackSpool(spool_root() if root is None else root)
    logger.info("callback-spool initialised at %s", _SPOOL.root)
    return _SPOOL


def get_spool() -> "CallbackSpool | None":
    """The process-wide spool, or ``None`` before boot wired one (every
    caller degrades explicitly rather than resolving paths against the CWD)."""
    return _SPOOL
