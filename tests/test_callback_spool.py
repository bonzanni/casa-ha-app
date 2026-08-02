"""``callback_spool.py`` — the ``/data/callbacks`` spool protocol
(dirs/ready/index layout, claim/TTL/publish, the mint contract,
sweep/recovery; INV-CB-002).

The protocol's two load-bearing properties are pinned here with real
concurrency, not just single-threaded sequences:

* **exactly-once consumption** — the claim is a ``link(2)`` publish-once, so
  two *processes* racing the same pending state produce exactly one winner;
* **never partially visible** — a result appears in ``results/`` only as a
  hard link to an already-written, already-fsynced inode, so a collector
  polling in a tight loop can never read a short or invalid record.

Time is constructed with ``os.utime`` (never ``time.sleep``): mtime is the
single clock, and every TTL/skew case is therefore deterministic.
"""
import errno
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest

import callback_spool as cs
from callback_spool import (
    PENDING_TTL_S,
    RESTORE_GRACE_S,
    RESULT_TTL_S,
    SKEW_S,
    TEMP_TTL_S,
    CallbackSpool,
    index_key,
    mint,
    state_hash,
)

PLUGIN = "acme"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def spool(tmp_path):
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    try:
        yield s
    finally:
        s.close()


def _pdir(spool, plugin=PLUGIN) -> Path:
    return Path(spool.root) / plugin


def _pending(spool, plugin=PLUGIN) -> Path:
    return _pdir(spool, plugin) / "pending"


def _results(spool, plugin=PLUGIN) -> Path:
    return _pdir(spool, plugin) / "results"


def _claims(spool, plugin=PLUGIN) -> Path:
    return _pdir(spool, plugin) / ".claims"


def _utime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _record(h: str) -> dict:
    return {"v": 1, "plugin": PLUGIN, "effective": f"plg-{PLUGIN}--oauth",
            "received_at": 1234567890, "raw_query": f"code=abc&state={h[:8]}",
            "query": [["code", "abc"], ["state", h[:8]]]}


def _put(path: Path, mtime: float, text: str = "{}") -> Path:
    path.write_text(text)
    _utime(path, mtime)
    return path


@pytest.fixture
def fs_events(monkeypatch):
    """Ordered syscall recorder for the protocol-sequence pins.

    ``callback_spool.os`` IS the stdlib ``os`` module, so these wrappers are
    global for the duration of one test; each delegates to the real syscall
    and ``monkeypatch`` restores them at teardown. Directory fsyncs are
    recorded by resolving the FD through ``/proc/self/fd`` so the *target*
    directory is visible in the trace, not an opaque integer.
    """
    events: list[tuple] = []
    real_fsync, real_link, real_unlink = os.fsync, os.link, os.unlink

    def _fd_path(fd):
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:  # pragma: no cover — /proc always present on Linux
            return f"fd:{fd}"

    def fsync(fd):
        events.append(("fsync", _fd_path(fd)))
        return real_fsync(fd)

    def link(src, dst, **kw):
        events.append(("link", os.fspath(src), os.fspath(dst)))
        return real_link(src, dst, **kw)

    def unlink(path, **kw):
        events.append(("unlink", os.fspath(path)))
        return real_unlink(path, **kw)

    monkeypatch.setattr(cs.os, "fsync", fsync)
    monkeypatch.setattr(cs.os, "link", link)
    monkeypatch.setattr(cs.os, "unlink", unlink)
    return events


def _idx(events, needle) -> int:
    """Index of the first matching event. A 3-tuple matches exactly; a
    2-tuple whose target contains ``/`` matches by path SUFFIX (fsync targets
    are resolved to absolute paths), otherwise exactly — so ``("unlink", h)``
    can never be satisfied by the earlier ``.tmp-<h>`` unlink."""
    kind, target = needle[0], needle[1]
    for i, ev in enumerate(events):
        if ev[0] != kind:
            continue
        if len(needle) == 3:
            if tuple(ev[1:]) == tuple(needle[1:]):
                return i
        elif "/" in target:
            if str(ev[1]).endswith(target):
                return i
        elif str(ev[1]) == target:
            return i
    raise AssertionError(f"event {needle!r} not in {events!r}")


def _idx_after(events, needle, after: int) -> int:
    """First match strictly after index *after* — the publish sequence unlinks
    the reserved temp name twice (defensively on entry, then after the link),
    so ordering assertions must name which one they mean."""
    return after + 1 + _idx(events[after + 1:], needle)


# ---------------------------------------------------------------------------
# directories / modes
# ---------------------------------------------------------------------------


def test_ensure_plugin_dirs_creates_0770_tree(spool):
    for sub in ("pending", "results", ".claims"):
        d = _pdir(spool) / sub
        assert d.is_dir()
        assert stat.S_IMODE(d.stat().st_mode) == 0o770
    assert stat.S_IMODE(_pdir(spool).stat().st_mode) == 0o770


def test_ensure_plugin_dirs_is_idempotent(spool):
    h = state_hash("keepme")
    _put(_pending(spool) / f"{h}.json", time.time())
    spool.ensure_plugin_dirs(PLUGIN)
    assert (_pending(spool) / f"{h}.json").exists()


def test_ensure_plugin_dirs_never_rewrites_a_valid_token(spool):
    """A VALID ``.dir-id`` is minted exactly once per directory life: any
    later pass leaves it byte-identical (an in-flight claim's captured token
    must stay comparable for the claim's whole life)."""
    token_path = _pdir(spool) / ".dir-id"
    before = token_path.read_bytes()
    spool.ensure_plugin_dirs(PLUGIN)
    spool.ensure_plugin_dirs(PLUGIN)
    assert token_path.read_bytes() == before


def test_ensure_plugin_dirs_repairs_a_malformed_token(spool):
    """A POSITIVELY malformed token (wrong grammar/size, or dir-shaped) is
    retired and re-minted — and an in-flight claim carrying the old token
    then fails closed at discard/publish like any other identity drift."""
    token_path = _pdir(spool) / ".dir-id"
    token_path.write_text("not-a-token")
    spool.ensure_plugin_dirs(PLUGIN)
    minted = token_path.read_bytes()
    assert len(minted) == 32 and minted != b"not-a-token"


def test_ensure_plugin_dirs_reprobes_under_the_repair_lock(spool, monkeypatch):
    """A STALE pre-lock probe (a concurrent pass minted a valid token between
    the probe and the repair lock) must not retire that token: the repair
    re-probes under the exclusive lock and skips when it finds a valid one
    (red case for the round-2 review finding)."""
    token_path = _pdir(spool) / ".dir-id"
    before = token_path.read_bytes()
    real = cs._classify_dir_token
    calls = {"n": 0}

    def stale_once(dir_fd):
        calls["n"] += 1
        if calls["n"] == 1:
            return None                      # the stale pre-lock view
        return real(dir_fd)

    monkeypatch.setattr(cs, "_classify_dir_token", stale_once)
    spool.ensure_plugin_dirs(PLUGIN)

    assert calls["n"] >= 2, "repair must re-probe under the lock"
    assert token_path.read_bytes() == before, "valid token must survive"


def test_ensure_plugin_dirs_aborts_on_an_unknowable_token_state(spool, monkeypatch):
    """A transient I/O failure mid-probe (EIO, descriptor pressure, a short
    read) proves NOTHING about the token — the repair path must abort loudly
    rather than retire what may be a valid, live token (red case for the
    round-1 review finding: a repair keyed on the gate's collapsed None)."""
    token_path = _pdir(spool) / ".dir-id"
    before = token_path.read_bytes()

    def eio(*a, **k):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(cs.os, "read", eio)
    with pytest.raises(OSError):
        spool.ensure_plugin_dirs(PLUGIN)
    monkeypatch.undo()

    assert token_path.read_bytes() == before, "token must survive the failure"


def test_ensure_plugin_dirs_refuses_unsafe_plugin_name(spool):
    with pytest.raises(ValueError):
        spool.ensure_plugin_dirs("../escape")


@pytest.mark.parametrize("reserved", [".index", ".hidden", ".", "..", "a/b", ""])
def test_ensure_plugin_dirs_refuses_reserved_and_dotted_names(spool, reserved):
    """A dot-prefixed spool dir would be created but then skipped by the
    plugin enumeration — never swept, recovered or GC'd."""
    with pytest.raises(ValueError):
        spool.ensure_plugin_dirs(reserved)


def test_no_plugin_scoped_operation_can_escape_the_root_with_dotdot(spool, tmp_path):
    """Red case for path traversal: every plugin-scoped entry point resolves
    its directory through the one guarded funnel, so ``..`` is refused rather
    than resolving above the pinned root."""
    outside = Path(spool.root).parent
    escape = "../" + outside.name          # non-component name
    for op in (lambda: spool.write_ready("..", {"v": 1}),
               lambda: spool.delete_ready(".."),
               lambda: spool.ensure_plugin_dirs(".."),
               lambda: spool.write_ready(escape, {"v": 1}),
               lambda: spool.delete_ready(escape)):
        with pytest.raises(ValueError):
            op()
    assert spool.claim("..", state_hash("s"), now=time.time()) is None
    assert spool.claim(escape, state_hash("s"), now=time.time()) is None

    assert not (outside / "ready.json").exists()
    assert list(outside.glob("*/ready.json")) == []


# ---------------------------------------------------------------------------
# mint (consumer contract; the reference helper lives here)
# ---------------------------------------------------------------------------


def test_mint_publishes_pending_file_0600(spool):
    p = mint(_pdir(spool), "state-one")
    assert p == _pending(spool) / f"{state_hash('state-one')}.json"
    assert json.loads(p.read_text()) == {"v": 1}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not list(_pending(spool).glob("*.part"))


def test_mint_reuse_is_a_hard_error(spool):
    mint(_pdir(spool), "state-one")
    with pytest.raises(FileExistsError):
        mint(_pdir(spool), "state-one")
    # the loser leaves no residue behind
    assert not list(_pending(spool).glob("*.part"))


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_consumes_pending_and_preserves_mint_mtime(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - 120)
    h = state_hash("s")

    claim = spool.claim(PLUGIN, h, now=now)

    assert claim is not None
    assert claim.plugin == PLUGIN and claim.state_hash == h
    assert claim.mtime == pytest.approx(now - 120, abs=0.01)
    assert not p.exists()
    assert (_claims(spool) / h).exists()
    # rename/link preserves the MINT mtime — the single clock
    assert (_claims(spool) / h).stat().st_mtime == pytest.approx(now - 120, abs=0.01)


def test_claim_of_never_minted_state_returns_none(spool):
    assert spool.claim(PLUGIN, state_hash("nope"), now=time.time()) is None


def test_claim_twice_returns_none_the_second_time(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    assert spool.claim(PLUGIN, h, now=now) is not None
    assert spool.claim(PLUGIN, h, now=now) is None


def test_claim_never_clobbers_an_existing_claim(spool):
    """Red case for publish-once on the CLAIM (a plain replacing rename
    survives every other race test here): a crash between the claim link and
    the pending unlink leaves BOTH names. A redirect arriving in that window
    must lose, leaving the original claim inode intact for the recovery pass
    — a replacing rename would destroy it and consume the pending twin."""
    now = time.time()
    h = state_hash("s")
    mint(_pdir(spool), "s")
    existing = _put(_claims(spool) / h, now - 60, '{"original": true}')
    ino = existing.stat().st_ino

    assert spool.claim(PLUGIN, h, now=now) is None

    assert existing.stat().st_ino == ino
    assert json.loads(existing.read_text()) == {"original": True}
    assert (_pending(spool) / f"{h}.json").exists(), "a loser consumes nothing"
    assert spool.in_flight() == set()


def test_claim_rejects_malformed_hash(spool):
    assert spool.claim(PLUGIN, "../../etc/passwd", now=time.time()) is None
    assert spool.claim(PLUGIN, "abc", now=time.time()) is None


def test_claim_unknown_plugin_returns_none(spool):
    assert spool.claim("ghost", state_hash("s"), now=time.time()) is None


def test_claim_of_expired_pending_returns_none_and_deletes(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - PENDING_TTL_S - 1)
    h = state_hash("s")

    assert spool.claim(PLUGIN, h, now=now) is None
    assert not p.exists()
    assert list(_claims(spool).iterdir()) == []


def test_claim_at_exactly_the_ttl_still_wins(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - PENDING_TTL_S)
    assert spool.claim(PLUGIN, state_hash("s"), now=now) is not None


def test_claim_of_future_mtime_pending_fails_closed(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now + SKEW_S + 1)
    h = state_hash("s")

    assert spool.claim(PLUGIN, h, now=now) is None
    assert not p.exists()
    assert list(_claims(spool).iterdir()) == []


def test_claim_within_the_skew_allowance_still_wins(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now + SKEW_S)
    assert spool.claim(PLUGIN, state_hash("s"), now=now) is not None


def test_claim_adds_and_publish_clears_the_in_flight_set(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=now)
    assert cs.in_flight_key(PLUGIN, h) in spool.in_flight()
    spool.publish_result(claim, _record(h))
    assert spool.in_flight() == set()


def test_discard_claim_removes_it_and_clears_in_flight(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=now)

    spool.discard_claim(claim)

    assert list(_claims(spool).iterdir()) == []
    assert spool.in_flight() == set()


# ---------------------------------------------------------------------------
# hostile inodes at protocol names (the FD/type discipline)
# ---------------------------------------------------------------------------


def test_claim_of_a_symlinked_pending_never_captures_the_target(spool, tmp_path):
    """``link(2)`` must not follow: a symlink planted at a pending name would
    otherwise be hard-linked to its TARGET, pulling an arbitrary outside file
    into the spool as a "claimed state"."""
    secret = tmp_path / "outside-secret"
    secret.write_text("not yours")
    h = state_hash("s")
    (_pending(spool) / f"{h}.json").symlink_to(secret)

    assert spool.claim(PLUGIN, h, now=time.time()) is None

    assert secret.read_text() == "not yours"
    assert secret.stat().st_nlink == 1, "the target must not have been linked"
    assert list(_claims(spool).iterdir()) == []


def test_claim_rejects_a_non_regular_pending_inode(spool):
    """A FIFO (or any non-regular inode) at a pending name is refused by the
    type gate, not merely by the symlink flag."""
    h = state_hash("s")
    os.mkfifo(_pending(spool) / f"{h}.json")

    assert spool.claim(PLUGIN, h, now=time.time()) is None

    assert list(_claims(spool).iterdir()) == []


def test_a_symlinked_plugin_dir_is_never_followed(spool, tmp_path):
    """Directory FDs are opened ``O_NOFOLLOW``: a symlink planted at a spool
    dir name must not redirect the protocol to a tree outside the root."""
    outside = tmp_path / "outside-spool"
    for sub in ("pending", "results", ".claims"):
        (outside / sub).mkdir(parents=True)
    mint(outside, "s")
    h = state_hash("s")
    (Path(spool.root) / "evil").symlink_to(outside)

    assert spool.claim("evil", h, now=time.time()) is None

    assert (outside / "pending" / f"{h}.json").exists(), "untouched"
    assert list((outside / ".claims").iterdir()) == []


def test_discard_claim_fails_closed_when_the_plugin_dir_was_replaced(spool):
    """Same identity gate as publish: after a removal + reinstall the name
    denotes a different directory, and a same-named claim there belongs to
    another flow."""
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)
    other = _put(_claims(spool) / h, time.time(), '{"someone-else": true}')

    spool.discard_claim(claim)

    assert other.exists(), "a claim in the recreated dir is not ours to remove"


def test_discard_claim_fails_closed_when_the_replaced_dir_reuses_the_inode(spool):
    """ext4 hands a freed inode number straight back, so a recreated plugin
    dir can carry the SAME ``(st_dev, st_ino)`` as the directory the claim
    was taken from — the stat pair alone cannot prove identity (the CI
    runners' ``/tmp`` is ext4, where this reuse is the common case, not a
    fluke). Forge that worst case by grafting the recreated dir's stat pair
    onto the old claim: the gate must still refuse."""
    import dataclasses
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)
    st = os.stat(_pdir(spool))
    reused = dataclasses.replace(claim, dir_dev=st.st_dev, dir_ino=st.st_ino)
    other = _put(_claims(spool) / h, time.time(), '{"someone-else": true}')

    spool.discard_claim(reused)

    assert other.exists(), "a claim in the recreated dir is not ours to remove"


# ---------------------------------------------------------------------------
# claim races — two threads AND two processes (INV-CB-002)
# ---------------------------------------------------------------------------


def test_claim_is_exactly_once_under_two_threads(spool):
    import threading

    now = time.time()
    hashes = [state_hash(f"t{i}") for i in range(40)]
    for i in range(40):
        mint(_pdir(spool), f"t{i}")

    won: dict[int, list[str]] = {0: [], 1: []}
    start = threading.Barrier(2)

    def worker(idx):
        start.wait()
        for h in hashes:
            if spool.claim(PLUGIN, h, now=now) is not None:
                won[idx].append(h)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allwon = won[0] + won[1]
    assert sorted(allwon) == sorted(hashes)
    assert len(allwon) == len(set(allwon))


def _claim_child(root, hashes, out_path, barrier):
    """fork child: race every hash, record the ones it won."""
    won = []
    s = CallbackSpool(root)
    now = time.time()
    barrier.wait()
    for h in hashes:
        if s.claim(PLUGIN, h, now=now) is not None:
            won.append(h)
    s.close()
    Path(out_path).write_text(json.dumps(won))
    os._exit(0)


def test_claim_is_exactly_once_under_two_processes(spool, tmp_path):
    hashes = [state_hash(f"p{i}") for i in range(60)]
    for i in range(60):
        mint(_pdir(spool), f"p{i}")

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    outs = [tmp_path / "won0.json", tmp_path / "won1.json"]
    procs = [ctx.Process(target=_claim_child,
                         args=(str(spool.root), hashes, str(outs[i]), barrier))
             for i in (0, 1)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0

    allwon = json.loads(outs[0].read_text()) + json.loads(outs[1].read_text())
    assert sorted(allwon) == sorted(hashes), "every pending state must be claimed"
    assert len(allwon) == len(set(allwon)), "a state was claimed twice"
    assert list(_pending(spool).iterdir()) == []


# ---------------------------------------------------------------------------
# publish_result
# ---------------------------------------------------------------------------


def _claimed(spool, state="s"):
    mint(_pdir(spool), state)
    h = state_hash(state)
    return h, spool.claim(PLUGIN, h, now=time.time())


def test_publish_result_writes_intact_0600_record_and_clears_everything(spool):
    h, claim = _claimed(spool)
    rec = _record(h)

    assert spool.publish_result(claim, rec) is True

    out = _results(spool) / f"{h}.json"
    assert json.loads(out.read_text()) == rec
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert list(_claims(spool).iterdir()) == [], "claim and temp must both be gone"


def test_publish_result_follows_the_exact_protocol_sequence(spool, fs_events):
    h, claim = _claimed(spool)
    del fs_events[:]  # only the publish sequence matters here

    assert spool.publish_result(claim, _record(h)) is True

    i_link = _idx(fs_events, ("link", f".tmp-{h}", f"{h}.json"))
    i_tmp_fsync = _idx(fs_events, ("fsync", f".claims/.tmp-{h}"))
    i_res_fsync = _idx_after(fs_events, ("fsync", "/results"), i_link)
    i_tmp_unlink = _idx_after(fs_events, ("unlink", f".tmp-{h}"), i_link)
    i_claim_unlink = _idx(fs_events, ("unlink", h))
    claims_fsyncs = [i for i, e in enumerate(fs_events)
                     if e[0] == "fsync" and str(e[1]).endswith("/.claims")]

    # write+fsync temp -> link -> fsync results/ -> unlink temp ->
    # fsync .claims/ -> unlink claim -> fsync .claims/
    assert i_tmp_fsync < i_link < i_res_fsync < i_tmp_unlink < i_claim_unlink
    assert len([c for c in claims_fsyncs if i_tmp_unlink < c < i_claim_unlink]) >= 1
    assert len([c for c in claims_fsyncs if c > i_claim_unlink]) >= 1


def test_publish_result_when_a_result_already_exists_is_an_anomaly(spool):
    h, claim = _claimed(spool)
    prior = _put(_results(spool) / f"{h}.json", time.time(), '{"prior": true}')

    assert spool.publish_result(claim, _record(h)) is False

    assert json.loads(prior.read_text()) == {"prior": True}, "never clobbered"
    assert list(_claims(spool).iterdir()) == [], "claim and temp removed anyway"
    assert spool.in_flight() == set()


def test_publish_result_reclaims_a_stale_temp_left_by_a_crash(spool):
    h, claim = _claimed(spool)
    _put(_claims(spool) / f".tmp-{h}", time.time() - 5, "garbage-not-json")

    assert spool.publish_result(claim, _record(h)) is True

    assert json.loads((_results(spool) / f"{h}.json").read_text()) == _record(h)
    assert list(_claims(spool).iterdir()) == []


def test_publish_result_fails_closed_when_the_plugin_dir_was_replaced(spool):
    """A concurrent plugin removal + reinstall between claim and publish
    yields a DIFFERENT directory inode; the result must not land in it."""
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)

    assert spool.publish_result(claim, _record(h)) is False
    assert list(_results(spool).iterdir()) == []


def test_publish_result_fails_closed_when_the_replaced_dir_reuses_the_inode(spool):
    """Same worst case as the discard twin: a recreated dir carrying a
    recycled ``(st_dev, st_ino)``. Identity must not rest on the stat pair."""
    import dataclasses
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)
    st = os.stat(_pdir(spool))
    reused = dataclasses.replace(claim, dir_dev=st.st_dev, dir_ino=st.st_ino)

    assert spool.publish_result(reused, _record(h)) is False
    assert list(_results(spool).iterdir()) == []


def test_publish_result_write_failure_leaves_the_claim_for_recovery(spool, monkeypatch):
    """A transient write failure must not silently eat the flow: the claim
    stays, the in-flight entry does NOT (or recovery would skip that claim for
    the rest of the process's life), and the next boot pass restores it."""
    h, claim = _claimed(spool)

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cs, "_write_new_file", boom)
    assert spool.publish_result(claim, _record(h)) is False
    monkeypatch.undo()

    assert (_claims(spool) / h).exists()
    assert spool.in_flight() == set()
    assert list(_results(spool).iterdir()) == []
    assert not (_claims(spool) / f".tmp-{h}").exists()

    report = spool.recovery_pass(now=time.time(), boot=True)
    assert report.restored == [(PLUGIN, h)]


def test_publish_result_refuses_an_unserializable_record(spool):
    h, claim = _claimed(spool)

    assert spool.publish_result(claim, {"bad": object()}) is False

    assert (_claims(spool) / h).exists(), "left for recovery, not eaten"
    assert spool.in_flight() == set()


def test_read_side_helpers(spool):
    h, claim = _claimed(spool)
    assert spool.list_results(PLUGIN) == []
    assert spool.has_result(PLUGIN, h) is False

    spool.publish_result(claim, _record(h))

    assert spool.list_results(PLUGIN) == [h]
    assert spool.has_result(PLUGIN, h) is True
    assert spool.has_result("ghost", h) is False
    assert spool.has_result(PLUGIN, "not-a-hash") is False
    assert spool.plugins() == [PLUGIN]


def test_result_mtime_is_its_final_write_time_not_the_mint_time(spool):
    """Each TTL runs off its own file's mtime: a long
    authorization flow must not expire its result on arrival."""
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - PENDING_TTL_S + 60)      # minted 29 minutes ago
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=now)

    assert spool.publish_result(claim, _record(h)) is True

    res_mtime = (_results(spool) / f"{h}.json").stat().st_mtime
    assert res_mtime == pytest.approx(time.time(), abs=5)
    assert res_mtime - claim.mtime > 60


# ---------------------------------------------------------------------------
# partial non-exposure — a collector racing the publisher (multiprocess)
# ---------------------------------------------------------------------------

_PAD = "x" * 8_000


def _expected_bytes(h):
    return json.dumps({"v": 1, "h": h, "pad": _PAD}).encode("utf-8")


def _publisher_child(root, states):
    s = CallbackSpool(root)
    for st in states:
        mint(Path(root) / PLUGIN, st)
        h = state_hash(st)
        claim = s.claim(PLUGIN, h, now=time.time())
        s.publish_result(claim, {"v": 1, "h": h, "pad": _PAD})
    s.close()
    os._exit(0)


def _reader_child(root, states, out_path):
    """Spin on the NEXT expected result name — so every observation lands in
    the publisher's write window for that exact file — and require the very
    first successful read to be the complete record. Any short, empty or
    half-written observation is a violation: a name may only appear once its
    content is already whole."""
    resdir = Path(root) / PLUGIN / "results"
    bad, reads, verified = [], 0, 0
    for st in states:
        h = state_hash(st)
        path, want = resdir / f"{h}.json", _expected_bytes(h)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                raw = path.read_bytes()
            except OSError:
                continue                     # not published yet — keep spinning
            reads += 1
            if raw == want:
                verified += 1
            else:
                bad.append(f"{h[:8]}: partial observation ({len(raw)} bytes)")
            break
        else:
            bad.append(f"{h[:8]}: never appeared")
    Path(out_path).write_text(json.dumps({"bad": bad, "reads": reads,
                                          "verified": verified}))
    os._exit(0)


def test_a_racing_collector_never_observes_a_partial_result(spool, tmp_path):
    states = [f"race{i}" for i in range(200)]
    out = tmp_path / "reader.json"

    ctx = multiprocessing.get_context("fork")
    reader = ctx.Process(target=_reader_child,
                         args=(str(spool.root), states, str(out)))
    writer = ctx.Process(target=_publisher_child, args=(str(spool.root), states))
    reader.start()
    writer.start()
    writer.join(60)
    reader.join(60)
    assert writer.exitcode == 0 and reader.exitcode == 0

    report = json.loads(out.read_text())
    assert report["bad"] == []
    assert report["verified"] == len(states)
    assert report["reads"] == len(states)


# ---------------------------------------------------------------------------
# recovery_pass
# ---------------------------------------------------------------------------


def test_boot_recovery_restores_a_young_unresulted_claim_with_its_mtime(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)

    report = spool.recovery_pass(now=now, boot=True)

    restored = _pending(spool) / f"{h}.json"
    assert restored.exists()
    assert restored.stat().st_mtime == pytest.approx(now - 300, abs=0.01)
    assert list(_claims(spool).iterdir()) == []
    assert report.restored == [(PLUGIN, h)]


def test_boot_recovery_unlinks_an_orphan_temp_before_restoring_its_claim(
        spool, fs_events):
    """Crash after the temp write, before the publish: the deterministic
    ``.tmp-<hash>`` name must be free again before the claim goes back to
    pending, or the retry finds the name occupied (spec r8 fold)."""
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    _put(_claims(spool) / f".tmp-{h}", now - 300, "half-written")
    del fs_events[:]

    report = spool.recovery_pass(now=now, boot=True)

    assert not (_claims(spool) / f".tmp-{h}").exists()
    assert (_pending(spool) / f"{h}.json").exists()
    assert report.temps_cleared == 1
    i_unlink_tmp = _idx(fs_events, ("unlink", f".tmp-{h}"))
    i_restore = _idx(fs_events, ("link", h, f"{h}.json"))
    assert i_unlink_tmp < i_restore
    # the temp's removal is durable before the claim is republished
    assert any(e[0] == "fsync" and str(e[1]).endswith("/.claims")
               for e in fs_events[i_unlink_tmp:i_restore])


def test_boot_recovery_reports_a_claim_with_a_result_for_nudge_then_removes_it(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    _put(_results(spool) / f"{h}.json", now - 10)

    report = spool.recovery_pass(now=now, boot=True)

    assert report.nudges == [(PLUGIN, h)]
    assert report.restored == []
    assert list(_claims(spool).iterdir()) == []
    assert not (_pending(spool) / f"{h}.json").exists(), "never re-mint a done flow"


def test_recovery_nudges_a_stale_claim_whose_result_was_already_published(spool):
    """A published result outranks the claim's age. The claim's mtime is the
    MINT time, so a slow authorization (minted 31 min ago, result written
    seconds before the crash) would be "stale" by that clock — dropping it
    loses the nudge while the credential sits live in results/ for its own
    15-minute TTL."""
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - PENDING_TTL_S - 60)
    result = _put(_results(spool) / f"{h}.json", now - 300)

    report = spool.recovery_pass(now=now, boot=True)

    assert report.nudges == [(PLUGIN, h)]
    assert report.dropped == []
    assert result.exists(), "the live result must not be stranded"
    assert list(_claims(spool).iterdir()) == []


def test_recovery_ignores_a_non_regular_result_when_deciding_a_nudge(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    os.mkfifo(_results(spool) / f"{h}.json")

    report = spool.recovery_pass(now=now, boot=True)

    assert report.nudges == []
    assert report.restored == [(PLUGIN, h)], "no real result ⇒ restore the flow"


def test_recovery_drops_a_stale_claim(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - PENDING_TTL_S - 1)

    report = spool.recovery_pass(now=now, boot=True)

    assert list(_claims(spool).iterdir()) == []
    assert not (_pending(spool) / f"{h}.json").exists()
    assert report.dropped == [(PLUGIN, h)]


def test_recovery_drops_a_future_mtime_claim(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now + SKEW_S + 1)

    spool.recovery_pass(now=now, boot=True)

    assert list(_claims(spool).iterdir()) == []
    assert not (_pending(spool) / f"{h}.json").exists()


def test_recovery_removes_unparseable_claims_dir_residue(spool):
    now = time.time()
    _put(_claims(spool) / "not-a-hash", now - 10)

    report = spool.recovery_pass(now=now, boot=True)

    assert list(_claims(spool).iterdir()) == []
    assert report.anomalies


def test_periodic_recovery_skips_an_in_flight_claim_that_boot_would_restore(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - 300)
    h = state_hash("s")
    spool.claim(PLUGIN, h, now=now)          # live handler: hash is in flight

    spool.recovery_pass(now=now, boot=False)
    assert (_claims(spool) / h).exists(), "a live claim must not be restored"
    assert not (_pending(spool) / f"{h}.json").exists()

    report = spool.recovery_pass(now=now, boot=True)
    assert report.restored == [(PLUGIN, h)]


def test_periodic_recovery_skips_an_in_flight_orphan_temp(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    spool.claim(PLUGIN, h, now=now)
    tmp = _put(_claims(spool) / f".tmp-{h}", now, "being-written")

    spool.recovery_pass(now=now, boot=False)

    assert tmp.exists(), "a live writer's temp must not be unlinked under it"


def test_periodic_recovery_skips_a_claim_younger_than_the_restore_grace(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - (RESTORE_GRACE_S - 10))

    spool.recovery_pass(now=now, boot=False)
    assert (_claims(spool) / h).exists()

    report = spool.recovery_pass(now=now, boot=True)
    assert report.restored == [(PLUGIN, h)]


def test_recovery_restore_does_not_clobber_a_republished_pending(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    live = _put(_pending(spool) / f"{h}.json", now, '{"live": true}')

    spool.recovery_pass(now=now, boot=True)

    assert json.loads(live.read_text()) == {"live": True}
    assert list(_claims(spool).iterdir()) == []


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_sweep_deletes_expired_and_future_entries_in_all_name_classes(spool):
    now = time.time()
    old_pending = _put(_pending(spool) / f"{state_hash('a')}.json", now - PENDING_TTL_S - 1)
    fut_pending = _put(_pending(spool) / f"{state_hash('b')}.json", now + SKEW_S + 1)
    old_part = _put(_pending(spool) / f"{state_hash('c')}.json.part", now - TEMP_TTL_S - 1)
    old_result = _put(_results(spool) / f"{state_hash('d')}.json", now - RESULT_TTL_S - 1)
    fut_result = _put(_results(spool) / f"{state_hash('e')}.json", now + SKEW_S + 1)
    old_collect = _put(_results(spool) / f".collect-{state_hash('f')}-abcd",
                       now - RESULT_TTL_S - 1)
    old_tmp = _put(_claims(spool) / f".tmp-{state_hash('g')}", now - TEMP_TTL_S - 1)

    report = spool.sweep(now=now)

    for p in (old_pending, fut_pending, old_part, old_result, fut_result,
              old_collect, old_tmp):
        assert not p.exists(), f"{p.name} should have been swept"
    assert report.total == 7


def test_sweep_keeps_everything_still_inside_its_own_ttl(spool):
    now = time.time()
    keep = [
        _put(_pending(spool) / f"{state_hash('a')}.json", now - PENDING_TTL_S + 60),
        _put(_pending(spool) / f"{state_hash('c')}.json.part", now - 10),
        _put(_results(spool) / f"{state_hash('d')}.json", now - RESULT_TTL_S + 60),
        _put(_results(spool) / f".collect-{state_hash('f')}-abcd", now - 30),
        _put(_claims(spool) / f".tmp-{state_hash('g')}", now - 10),
    ]

    report = spool.sweep(now=now)

    assert all(p.exists() for p in keep)
    assert report.total == 0


def test_sweep_does_not_touch_bare_claims(spool):
    """Claims are the recovery pass's business — a sweep that deleted a young
    claim would silently eat an in-flight authorization."""
    now = time.time()
    # Older than the temp/`.part` TTL (so a class confusion in the sweep WOULD
    # delete it) but still well inside the pending TTL, i.e. exactly the claim
    # the recovery pass must be allowed to restore.
    claim = _put(_claims(spool) / state_hash("s"), now - (TEMP_TTL_S + 600))

    spool.sweep(now=now)

    assert claim.exists()


def test_sweep_removes_unparseable_and_non_regular_entries(spool):
    now = time.time()
    junk = _put(_pending(spool) / "not-a-hash.json", now)
    subdir = _pending(spool) / "a-directory"
    subdir.mkdir()

    report = spool.sweep(now=now)

    assert not junk.exists()
    assert not subdir.exists()
    assert len(report.anomalies) == 2


def test_sweep_caps_pending_at_256_oldest_first(spool):
    now = time.time()
    made = []
    for i in range(300):
        made.append(_put(_pending(spool) / f"{state_hash(str(i))}.json", now - 300 + i))

    report = spool.sweep(now=now)

    left = sorted(p.name for p in _pending(spool).iterdir())
    assert len(left) == 256
    survivors = {p.name for p in made[-256:]}
    assert set(left) == survivors, "the 44 OLDEST must be the ones deleted"
    assert report.capped == [f"{PLUGIN}/pending"]


def test_sweep_counts_part_files_toward_the_pending_cap(spool):
    """A consumer looping on a failing publish leaves `.part` files in the same
    directory; a cap that ignored them would never bound /data."""
    now = time.time()
    for i in range(150):
        _put(_pending(spool) / f"{state_hash(str(i))}.json", now - 100 + i)
    for i in range(150):
        _put(_pending(spool) / f"{state_hash('p' + str(i))}.json.part", now - 60)

    report = spool.sweep(now=now)

    assert len(list(_pending(spool).iterdir())) == 256
    assert report.capped == [f"{PLUGIN}/pending"]


def test_sweep_removes_stale_ready_and_index_staging_residue(spool):
    """`_replace_json` residue from a crash between staging and the rename is
    swept nowhere else — it would otherwise live forever."""
    now = time.time()
    spool.write_index_entry("/artifacts/acme", {"plugin": PLUGIN})
    ready_tmp = _put(_pdir(spool) / ".ready.json.tmp-99-deadbeef", now - TEMP_TTL_S - 1)
    index_tmp = _put(Path(spool.root) / ".index" / ".k.json.tmp-99-deadbeef",
                     now - TEMP_TTL_S - 1)
    fresh = _put(_pdir(spool) / ".ready.json.tmp-99-cafe", now - 5)
    spool.write_ready(PLUGIN, {"v": 1})
    entry = Path(spool.root) / ".index" / f"{index_key('/artifacts/acme')}.json"

    report = spool.sweep(now=now)

    assert not ready_tmp.exists() and not index_tmp.exists()
    assert fresh.exists(), "a temp inside its age window belongs to a live writer"
    assert (_pdir(spool) / "ready.json").exists(), "never the published marker"
    assert entry.exists(), "never a published index entry"
    assert report.deleted_temps == 2


def test_sweep_caps_results_at_256_oldest_first(spool):
    now = time.time()
    for i in range(260):
        _put(_results(spool) / f"{state_hash(str(i))}.json", now - 300 + i)

    report = spool.sweep(now=now)

    assert len(list(_results(spool).iterdir())) == 256
    assert report.capped == [f"{PLUGIN}/results"]


# ---------------------------------------------------------------------------
# gc_orphan_dirs (gated GC)
# ---------------------------------------------------------------------------


def _quiesce(path: Path, when: float) -> None:
    """Age a whole spool dir tree (deepest first, so a parent's mtime is not
    bumped by a child's utime)."""
    for p in sorted(path.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        _utime(p, when)
    _utime(path, when)


DAY = 24 * 3600


def test_gc_is_a_noop_when_the_registry_did_not_load_valid(spool):
    """Red case: an unreadable registry must never vaporize spool dirs — a
    membership set built from a failed load would delete EVERY plugin's
    in-flight authorizations."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _quiesce(_pdir(spool), now - 5 * DAY)

    assert spool.gc_orphan_dirs(registry_valid=False, member_plugins=set(), now=now) == []

    assert _pdir(spool, "ghost").is_dir()
    assert _pdir(spool).is_dir()


def test_gc_removes_a_quiescent_orphan_dir(spool):
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)

    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins={PLUGIN},
                                   now=now)

    assert removed == ["ghost"]
    assert not _pdir(spool, "ghost").exists()
    assert _pdir(spool).is_dir(), "a registry member is never touched"


def test_gc_skips_a_dir_that_was_active_within_24h(spool):
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 3 * 3600)

    assert spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                now=now) == []
    assert _pdir(spool, "ghost").is_dir()


def test_gc_skips_a_dir_holding_an_entry_younger_than_the_pending_ttl(spool):
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _put(_pdir(spool, "ghost") / "results" / f"{state_hash('x')}.json", now - 60)
    _utime(_pdir(spool, "ghost") / "results", now - 5 * DAY)
    _utime(_pdir(spool, "ghost"), now - 5 * DAY)

    assert spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                now=now) == []
    assert _pdir(spool, "ghost").is_dir()


def test_gc_never_touches_the_index_dir(spool):
    now = time.time()
    spool.write_index_entry("/opt/artifacts/acme", {"plugin": PLUGIN})
    _quiesce(Path(spool.root) / ".index", now - 5 * DAY)

    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=now)

    assert ".index" not in removed
    assert (Path(spool.root) / ".index").is_dir()


# ---------------------------------------------------------------------------
# ready.json / .index ordering helpers
# ---------------------------------------------------------------------------


def test_write_ready_publishes_atomically_and_fsyncs_the_dir(spool, fs_events):
    payload = {"v": 1, "base_url": "https://x.example",
               "callbacks": {"oauth": {"effective": "plg-acme--oauth"}}}

    spool.write_ready(PLUGIN, payload)

    ready = _pdir(spool) / "ready.json"
    assert json.loads(ready.read_text()) == payload
    assert stat.S_IMODE(ready.stat().st_mode) == 0o600
    assert not [p for p in _pdir(spool).iterdir() if p.name.startswith(".ready")]
    assert any(e[0] == "fsync" and str(e[1]).endswith(f"/{PLUGIN}")
               for e in fs_events), "the marker's directory entry must be durable"


def test_write_ready_replaces_a_previous_marker(spool):
    spool.write_ready(PLUGIN, {"v": 1, "callbacks": {}})
    spool.write_ready(PLUGIN, {"v": 1, "callbacks": {"oauth": {}}})
    assert json.loads((_pdir(spool) / "ready.json").read_text())["callbacks"]


def test_delete_ready_removes_and_fsyncs_before_the_overlay_swap(spool, fs_events):
    spool.write_ready(PLUGIN, {"v": 1})
    del fs_events[:]

    spool.delete_ready(PLUGIN)

    assert not (_pdir(spool) / "ready.json").exists()
    i_unlink = _idx(fs_events, ("unlink", "ready.json"))
    assert any(e[0] == "fsync" and str(e[1]).endswith(f"/{PLUGIN}")
               for e in fs_events[i_unlink:]), "unrouting fsyncs the deletion"


def test_delete_ready_is_idempotent(spool):
    spool.delete_ready(PLUGIN)
    spool.delete_ready("never-existed")


def test_index_entry_roundtrip_and_delete(spool, fs_events):
    payload = {"plugin": PLUGIN, "ready": {"v": 1}}
    art = "/opt/artifacts/acme"

    spool.write_index_entry(art, payload)

    entry = Path(spool.root) / ".index" / f"{index_key(art)}.json"
    assert json.loads(entry.read_text()) == payload
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert any(e[0] == "fsync" and str(e[1]).endswith("/.index") for e in fs_events)

    del fs_events[:]
    spool.delete_index_entry(art)
    assert not entry.exists()
    assert any(e[0] == "fsync" and str(e[1]).endswith("/.index") for e in fs_events)


def test_creating_the_index_dir_fsyncs_the_root(spool, fs_events):
    """The directory entry must be durable before an entry inside it is —
    otherwise a power crash can keep the entry's inode and lose the directory
    that names it."""
    root = os.path.realpath(spool.root)

    spool.write_index_entry("/artifacts/acme", {"plugin": PLUGIN})

    assert any(e[0] == "fsync" and str(e[1]) == root for e in fs_events)


def test_delete_index_entry_is_idempotent(spool):
    spool.delete_index_entry("/nowhere")


def test_index_key_resolves_symlinks(tmp_path):
    real = tmp_path / "real-artifact"
    real.mkdir()
    link = tmp_path / "linked-artifact"
    link.symlink_to(real)

    assert index_key(str(link)) == index_key(str(real))
    assert index_key(str(real)) != index_key(str(tmp_path / "other"))
    assert len(index_key(str(real))) == 64


def test_state_hash_is_sha256_hex():
    import hashlib
    assert state_hash("abc") == hashlib.sha256(b"abc").hexdigest()


# ---------------------------------------------------------------------------
# closed instance fails closed (never a dir_fd=None, CWD-relative syscall)
# ---------------------------------------------------------------------------


def test_operations_do_not_leak_file_descriptors(spool):
    """Every path here opens directory FDs by hand; one missed ``close`` on an
    error branch would exhaust the process's FDs over an uptime."""
    def cycle(i):
        now = time.time()
        mint(_pdir(spool), f"fd{i}")
        h = state_hash(f"fd{i}")
        claim = spool.claim(PLUGIN, h, now=now)
        spool.publish_result(claim, _record(h))
        spool.write_ready(PLUGIN, {"v": 1})
        spool.write_index_entry(f"/artifacts/{i}", {"plugin": PLUGIN})
        spool.delete_index_entry(f"/artifacts/{i}")
        spool.delete_ready(PLUGIN)
        spool.sweep(now=now)
        spool.recovery_pass(now=now, boot=True)
        spool.gc_orphan_dirs(registry_valid=True, member_plugins={PLUGIN}, now=now)
        spool.claim("ghost", h, now=now)          # error branches too
        spool.has_result(PLUGIN, h)
        spool.list_results(PLUGIN)

    cycle(0)                                       # warm-up (lazy imports etc.)
    before = len(os.listdir("/proc/self/fd"))
    for i in range(1, 25):
        cycle(i)
    assert len(os.listdir("/proc/self/fd")) <= before


def test_a_closed_spool_refuses_every_operation(tmp_path):
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    s.close()
    s.close()  # idempotent

    assert s.claim(PLUGIN, state_hash("s"), now=time.time()) is None
    with pytest.raises(cs.SpoolClosed):
        s.ensure_plugin_dirs(PLUGIN)
    with pytest.raises(cs.SpoolClosed):
        s.write_ready(PLUGIN, {"v": 1})


# ---------------------------------------------------------------------------
# durable published-marker inventory (published_plugins / index_keys /
# delete_index_key) — the reconciler's on-disk truth
# ---------------------------------------------------------------------------


def test_published_plugins_lists_only_dirs_with_a_ready_marker(spool):
    spool.ensure_plugin_dirs("other")
    assert spool.published_plugins() == []          # dirs exist, no markers yet
    spool.write_ready(PLUGIN, {"v": 1})
    assert spool.published_plugins() == [PLUGIN]     # only the marked dir


def test_index_keys_lists_published_keys_only(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    assert spool.index_keys() == []
    spool.write_index_entry(str(art), {"v": 1})
    key = index_key(str(art))
    assert spool.index_keys() == [key]


def test_delete_index_key_retires_one_entry(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})
    key = index_key(str(art))
    entry = Path(spool.root) / ".index" / f"{key}.json"
    assert entry.is_file()

    spool.delete_index_key(key)
    assert not entry.exists()
    assert spool.index_keys() == []
    spool.delete_index_key(key)                      # idempotent — no raise


def test_delete_index_key_refuses_a_non_hash_key(spool):
    with pytest.raises(ValueError):
        spool.delete_index_key("../escape")


def test_durable_inventory_methods_are_empty_on_a_closed_spool(tmp_path):
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    s.write_ready(PLUGIN, {"v": 1})
    s.close()
    assert s.published_plugins() == []
    assert s.index_keys() == []
    with pytest.raises(cs.SpoolClosed):
        s.delete_index_key("0" * 64)


# ---------------------------------------------------------------------------
# three-state marker reader (read_marker / read_index_marker) — the durable
# on-disk truth the reconciler drives its paired transaction from. ABSENT and
# INVALID are DISTINCT: a stale-but-unreadable marker is republished, never
# mistaken for absent.
# ---------------------------------------------------------------------------


def test_read_marker_absent_when_no_file(spool):
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.ABSENT and m.payload is None
    # A plugin dir that does not exist at all is ABSENT, not INVALID.
    assert spool.read_marker("never-made").state is cs.MarkerState.ABSENT


def test_read_marker_present_returns_payload(spool):
    payload = {"v": 1, "base_url": "https://x.example", "callbacks": {}}
    spool.write_ready(PLUGIN, payload)
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.PRESENT
    assert m.payload == payload


def test_canonical_marker_bytes_is_sorted_compact_utf8():
    """The shared canonical form: sorted keys, most-compact separators, no
    ASCII-escaping, UTF-8 encoded."""
    b = cs.canonical_marker_bytes({"b": 1, "a": "é"})
    assert b == '{"a":"é","b":1}'.encode("utf-8")


def test_written_marker_is_byte_identical_to_canonical(spool):
    """Writer and compare share the helper: a marker casa writes is byte-for-byte
    canonical_marker_bytes(payload), and the reader exposes those exact raw bytes
    — so a second immediate pass sees it unchanged (no churn)."""
    payload = {"v": 1, "base_url": "https://x.example", "callbacks": {}}
    spool.write_ready(PLUGIN, payload)
    on_disk = (_pdir(spool) / "ready.json").read_bytes()
    assert on_disk == cs.canonical_marker_bytes(payload)
    m = spool.read_marker(PLUGIN)
    assert m.raw == cs.canonical_marker_bytes(payload)


def test_read_index_marker_roundtrip(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    assert spool.read_index_marker(str(art)).state is cs.MarkerState.ABSENT
    payload = {"v": 1, "plugin_dir": PLUGIN}
    spool.write_index_entry(str(art), payload)
    m = spool.read_index_marker(str(art))
    assert m.state is cs.MarkerState.PRESENT and m.payload == payload


def test_read_marker_of_a_fifo_is_invalid_and_never_blocks(spool):
    """A FIFO at ready.json (a swapped-in pipe) must be INVALID, and the read
    must return IMMEDIATELY — O_NONBLOCK + the S_ISREG gate mean it is never
    opened for a blocking read. If this hangs, the test times out."""
    os.mkfifo(_pdir(spool) / "ready.json")
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.INVALID and m.payload is None


def test_read_marker_of_an_oversized_file_is_invalid(spool):
    (_pdir(spool) / "ready.json").write_bytes(
        b'{"v":1,"pad":"' + b"p" * (cs.MARKER_STATE_MAX_BYTES + 16) + b'"}')
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_garbage_json_is_invalid(spool):
    (_pdir(spool) / "ready.json").write_text("{not valid json", encoding="utf-8")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_a_non_object_is_invalid(spool):
    (_pdir(spool) / "ready.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_a_symlink_is_invalid(spool, tmp_path):
    """O_NOFOLLOW: a symlinked ready.json is INVALID (never followed to an
    outside inode), not ABSENT."""
    target = tmp_path / "outside.json"
    target.write_text('{"v": 1}', encoding="utf-8")
    os.symlink(target, _pdir(spool) / "ready.json")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_a_directory_is_invalid(spool):
    os.mkdir(_pdir(spool) / "ready.json")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_index_marker_of_a_fifo_is_invalid_and_never_blocks(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})       # creates the .index dir
    entry = Path(spool.root) / cs.INDEX_DIR / f"{index_key(str(art))}.json"
    entry.unlink()
    os.mkfifo(entry)
    assert spool.read_index_marker(str(art)).state is cs.MarkerState.INVALID


def test_read_marker_of_deeply_nested_json_is_invalid_and_never_raises(spool):
    """The three-state reader is TOTAL: a body of 60k opening brackets fits the
    64 KiB size cap but makes ``json.loads`` raise ``RecursionError`` (not a
    ``ValueError``) — the reader must still return INVALID, never propagate."""
    (_pdir(spool) / "ready.json").write_bytes(b"[" * 60000)
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.INVALID and m.payload is None


# ---------------------------------------------------------------------------
# type-safe marker retirement + total enumeration (a non-regular ready.json /
# index entry must be ENUMERATED and RETIRED, never left to block republish)
# ---------------------------------------------------------------------------


def _make_dir(p):  os.mkdir(p)
def _make_fifo(p): os.mkfifo(p)
def _make_symlink(p): os.symlink("/nonexistent-target", p)


_NON_REGULAR = [
    pytest.param(_make_dir, id="dir"),
    pytest.param(_make_fifo, id="fifo"),
    pytest.param(_make_symlink, id="symlink"),
]


@pytest.mark.parametrize("maker", _NON_REGULAR)
def test_published_plugins_enumerates_a_non_regular_ready_marker(spool, maker):
    """An invalid ready.json (dir/FIFO/symlink) is an orphan that MUST be
    enumerated for retirement — omitting it (the old S_ISREG gate) would leave
    the invalid marker to survive forever and block republication."""
    maker(_pdir(spool) / "ready.json")
    assert spool.published_plugins() == [PLUGIN]


@pytest.mark.parametrize("maker", _NON_REGULAR)
def test_delete_ready_retires_a_marker_of_any_type(spool, maker):
    """A raw unlink FAILS on a directory-shaped marker; the type-aware retire
    removes any type and reports the entry now-absent."""
    maker(_pdir(spool) / "ready.json")
    assert spool.delete_ready(PLUGIN) is True
    assert not os.path.lexists(_pdir(spool) / "ready.json")   # incl. no symlink
    assert spool.published_plugins() == []


def test_delete_ready_of_an_absent_marker_reports_absent(spool):
    assert spool.delete_ready(PLUGIN) is True                 # nothing there


def test_delete_ready_surfaces_a_failed_removal(spool, monkeypatch):
    """A genuinely-failing removal must NOT be reported as absent — the caller
    surfaces it rather than assuming both-absent."""
    (_pdir(spool) / "ready.json").write_text("{}")
    monkeypatch.setattr(cs, "_remove_entry", lambda *a, **k: False)
    assert spool.delete_ready(PLUGIN) is False                # not swallowed
    assert (_pdir(spool) / "ready.json").exists()             # survived


def _lstat_raising(errnum, *, on_call=None):
    """Wrap the real ``os.lstat`` so a lstat of ``ready.json`` raises *errnum*
    (on the Nth such call, if *on_call* is given; else every call)."""
    real = os.lstat
    seen = {"n": 0}

    def flaky(path, *, dir_fd=None):
        if path == "ready.json":
            seen["n"] += 1
            if on_call is None or seen["n"] == on_call:
                raise OSError(errnum, os.strerror(errnum))
        return real(path, dir_fd=dir_fd)

    return flaky


def test_retire_pre_removal_metadata_error_is_not_read_as_absent(
    spool, monkeypatch,
):
    """A non-ENOENT failure on the PRE-removal probe (here EIO) must NOT be
    treated as 'already absent' — retirement reports FAILURE and the marker,
    never even removed, survives."""
    (_pdir(spool) / "ready.json").write_text("{}")
    monkeypatch.setattr(cs.os, "lstat", _lstat_raising(errno.EIO))
    assert spool.delete_ready(PLUGIN) is False
    assert (_pdir(spool) / "ready.json").exists()             # untouched


def test_retire_confirmation_metadata_error_reports_failure(spool, monkeypatch):
    """When the POST-removal RE-CONFIRMATION lstat raises EACCES, removal
    success cannot be confirmed, so retirement reports FAILURE (not a silent
    false success) — the reconcile then surfaces callback_spool_error."""
    (_pdir(spool) / "ready.json").write_text("{}")
    # First lstat (pre-removal probe) succeeds; the second (confirmation) raises.
    monkeypatch.setattr(cs.os, "lstat",
                        _lstat_raising(errno.EACCES, on_call=2))
    assert spool.delete_ready(PLUGIN) is False


def test_retire_confirmation_enoent_is_success(spool, monkeypatch):
    """A real removal whose confirming lstat sees ENOENT (the entry is gone) is
    reported as success — ENOENT is the ONLY 'absent' outcome."""
    (_pdir(spool) / "ready.json").write_text("{}")
    # Force the confirmation lstat to raise FileNotFoundError explicitly, even
    # though the real removal already made it ENOENT — success either way.
    monkeypatch.setattr(cs.os, "lstat",
                        _lstat_raising(errno.ENOENT, on_call=2))
    assert spool.delete_ready(PLUGIN) is True
    assert not (_pdir(spool) / "ready.json").exists()


def test_marker_lstat_is_three_valued(spool):
    """Unit pin: ENOENT ⇒ None (absent), any other OSError ⇒ _LSTAT_ERROR
    (unknown), a real entry ⇒ its stat_result (present)."""
    pfd = os.open(_pdir(spool), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert cs._marker_lstat("ready.json", pfd) is None      # ENOENT
        (_pdir(spool) / "ready.json").write_text("{}")
        st = cs._marker_lstat("ready.json", pfd)
        assert st is not None and st is not cs._LSTAT_ERROR      # present
    finally:
        os.close(pfd)


@pytest.mark.parametrize("maker", _NON_REGULAR)
def test_index_keys_enumerates_and_delete_retires_a_non_regular_entry(
    spool, tmp_path, maker,
):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})       # creates the .index dir
    key = index_key(str(art))
    entry = Path(spool.root) / cs.INDEX_DIR / f"{key}.json"
    entry.unlink()
    maker(entry)                                      # corrupt into a non-regular
    assert key in spool.index_keys()                  # still enumerated
    assert spool.delete_index_key(key) is True
    assert not os.path.lexists(entry)
    assert spool.index_keys() == []


def test_delete_index_entry_retires_a_directory_shaped_entry(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})
    entry = Path(spool.root) / cs.INDEX_DIR / f"{index_key(str(art))}.json"
    entry.unlink()
    os.mkdir(entry)                                   # directory-shaped entry
    assert spool.delete_index_entry(str(art)) is True
    assert not entry.exists()
