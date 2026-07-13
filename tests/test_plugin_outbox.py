"""plugin_outbox — FD-based, TOCTOU-safe claim/capture/sweep (v0.73.0, spec §3.4)."""
from __future__ import annotations

import errno
import os
import socket
import stat

import pytest

import plugin_outbox
from plugin_outbox import MAX_AGE_S, OutboxError, PluginOutbox

pytestmark = pytest.mark.unit

PDF = b"%PDF-1.7\n" + b"x" * 200
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100


@pytest.fixture
def outbox(tmp_path):
    root = tmp_path / "plugin-outbox"
    ob = PluginOutbox(str(root))
    yield ob
    ob.close()


def _write_outbox_file(outbox_root: str, name: str, data: bytes) -> str:
    p = os.path.join(outbox_root, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def _claim_of(outbox, name, data):
    src = _write_outbox_file(outbox._root_realpath, name, data)
    return outbox.claim(src)


# ---------------------------------------------------------------------------
# init + claim + remove_claim
# ---------------------------------------------------------------------------


def test_init_creates_dirs_0770_and_fds(tmp_path):
    root = tmp_path / "ob"
    ob = PluginOutbox(str(root))
    try:
        assert (root).is_dir()
        assert (root / ".claims").is_dir()
        assert stat.S_IMODE(os.stat(root).st_mode) == 0o770
        assert stat.S_IMODE(os.stat(root / ".claims").st_mode) == 0o770
        assert isinstance(ob._outbox_dirfd, int) and isinstance(ob._claims_dirfd, int)
    finally:
        ob.close()


def test_claim_moves_file_into_claims_and_returns_name(outbox):
    src = _write_outbox_file(outbox._root_realpath, "invoice-2026-07-abc.pdf", PDF)
    name = outbox.claim(src)
    assert "-" in name                              # <epoch_ms>-<uuid4hex>
    assert not os.path.exists(src)                  # original path is gone (claimed)
    assert os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_claim_outside_outbox_refused(outbox, tmp_path):
    other = tmp_path / "secret"
    other.write_bytes(b"nope")
    with pytest.raises(OutboxError) as ei:
        outbox.claim(str(other))
    assert ei.value.kind == "outside_outbox"


def test_claim_parent_traversal_refused(outbox):
    bad = os.path.join(outbox._root_realpath, "..", "secret.pdf")
    with pytest.raises(OutboxError) as ei:
        outbox.claim(bad)
    assert ei.value.kind == "outside_outbox"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b.pdf", "a\x00b.pdf", "a\nb.pdf"])
def test_claim_bad_basename_refused(outbox, bad):
    p = outbox._root_realpath + "/" + bad
    with pytest.raises(OutboxError) as ei:
        outbox.claim(p)
    assert ei.value.kind in ("bad_name", "outside_outbox")


def test_claim_missing_source(outbox):
    p = os.path.join(outbox._root_realpath, "gone.pdf")
    with pytest.raises(OutboxError) as ei:
        outbox.claim(p)
    assert ei.value.kind == "missing"


def test_claim_bare_basename_refused(outbox):
    # A bare basename (empty dirname) is refused deterministically (not CWD-dependent).
    with pytest.raises(OutboxError) as ei:
        outbox.claim("bare.pdf")
    assert ei.value.kind == "outside_outbox"


def test_claim_non_enoent_rename_is_guard_error(outbox, monkeypatch):
    src = _write_outbox_file(outbox._root_realpath, "x.pdf", PDF)

    def fake_rename(*a, **k):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(plugin_outbox.os, "rename", fake_rename)
    with pytest.raises(OutboxError) as ei:
        outbox.claim(src)
    assert ei.value.kind == "guard_error"


def test_claim_race_one_winner(outbox):
    src = _write_outbox_file(outbox._root_realpath, "race.pdf", PDF)
    name1 = outbox.claim(src)
    with pytest.raises(OutboxError) as ei:
        outbox.claim(src)                            # loser: source already renamed away
    assert ei.value.kind == "missing"
    assert os.path.exists(os.path.join(outbox._claims_realpath, name1))


def test_claim_concurrent_threads_one_winner(outbox):
    import threading
    src = _write_outbox_file(outbox._root_realpath, "conc.pdf", PDF)
    results: list = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()                                # maximise contention
        try:
            results.append(("ok", outbox.claim(src)))
        except OutboxError as e:
            results.append(("err", e.kind))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1 and errs[0][1] == "missing"
    outbox.remove_claim(oks[0][1])


def test_remove_claim_file(outbox):
    src = _write_outbox_file(outbox._root_realpath, "x.pdf", PDF)
    name = outbox.claim(src)
    outbox.remove_claim(name)
    assert not os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_remove_claim_directory(outbox):
    d = os.path.join(outbox._root_realpath, "weird.pdf")
    os.mkdir(d)
    with open(os.path.join(d, "inner"), "wb") as fh:
        fh.write(b"z")
    name = outbox.claim(d)
    outbox.remove_claim(name)
    assert not os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_init_outbox_singleton(tmp_path):
    root = tmp_path / "singleton-ob"
    ob = plugin_outbox.init_outbox(str(root))
    try:
        assert plugin_outbox.get_outbox() is ob
    finally:
        ob.close()
        plugin_outbox._OUTBOX = None


def test_closed_outbox_fails_closed_not_cwd(tmp_path, monkeypatch):
    # Regression (Sol diff review): after close(), the dir-FDs are None; a
    # dir_fd=None op resolves relative to the process CWD (fail-OPEN). Operations
    # on a closed outbox MUST fail CLOSED and touch NOTHING — never grab a
    # same-named CWD file.
    ob = PluginOutbox(str(tmp_path / "ob"))
    src = _write_outbox_file(ob._root_realpath, "invoice.pdf", PDF)
    ob.close()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "invoice.pdf").write_bytes(b"CWD-FILE")
    monkeypatch.chdir(cwd)
    with pytest.raises(OutboxError) as ei:
        ob.claim(src)
    assert ei.value.kind == "guard_error"
    assert (cwd / "invoice.pdf").read_bytes() == b"CWD-FILE"   # CWD file untouched
    assert os.path.exists(src)                                  # outbox file untouched
    with pytest.raises(OutboxError):
        ob.capture("anything", "document")                     # capture fail-closed too
    assert ob.sweep_once(10_000_000_000_000) == 0              # sweep no-op when closed


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def test_capture_returns_bytes_for_valid_pdf(outbox):
    name = _claim_of(outbox, "ok.pdf", PDF)
    got = outbox.capture(name, "document")
    assert got == PDF


def test_capture_magic_mismatch_pdf_as_photo(outbox):
    name = _claim_of(outbox, "ok.pdf", PDF)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "photo")
    assert ei.value.kind == "magic_mismatch"


def test_capture_photo_ok(outbox):
    name = _claim_of(outbox, "p.jpg", JPEG)
    assert outbox.capture(name, "photo") == JPEG


def test_capture_symlink_refused_via_nofollow(outbox, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"%PDF-secret")
    link = os.path.join(outbox._root_realpath, "link.pdf")
    os.symlink(str(secret), link)                    # symlink lives in the outbox
    name = outbox.claim(link)                          # rename moves the symlink itself
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "not_regular"
    outbox.remove_claim(name)


def test_capture_fifo_refused(outbox):
    p = os.path.join(outbox._root_realpath, "pipe.pdf")
    os.mkfifo(p)
    name = outbox.claim(p)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "not_regular"
    outbox.remove_claim(name)


def test_capture_hardlink_refused(outbox, tmp_path):
    target = tmp_path / "outside.pdf"
    target.write_bytes(PDF)
    link = os.path.join(outbox._root_realpath, "hard.pdf")
    os.link(str(target), link)                        # nlink == 2
    name = outbox.claim(link)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "multi_link"
    outbox.remove_claim(name)


def test_capture_oversize_refused(outbox):
    big = b"%PDF-" + b"x" * (10 * 1024 * 1024 + 5)     # > 10 MB photo cap
    name = _claim_of(outbox, "big.jpg", b"\xff\xd8\xff" + big)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "photo")
    assert ei.value.kind == "too_large"
    outbox.remove_claim(name)


def test_capture_directory_typed_claim_is_not_regular(outbox):
    d = os.path.join(outbox._root_realpath, "dir.pdf")
    os.mkdir(d)
    name = outbox.claim(d)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "not_regular"
    outbox.remove_claim(name)                          # must rmtree cleanly
    assert not os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_capture_empty_file_magic_mismatch(outbox):
    name = _claim_of(outbox, "empty.pdf", b"")
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")               # accepts(b"") is False, no crash
    assert ei.value.kind == "magic_mismatch"
    outbox.remove_claim(name)


def test_capture_jpeg_as_document_magic_mismatch(outbox):
    name = _claim_of(outbox, "j.jpg", JPEG)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "magic_mismatch"
    outbox.remove_claim(name)


def test_capture_shrink_is_guard_error(outbox, monkeypatch):
    name = _claim_of(outbox, "s.pdf", PDF)
    # fstat sees the real size, but the read returns fewer bytes -> integrity fault.
    monkeypatch.setattr(plugin_outbox, "_read_capped", lambda fd, cap: b"%PDF")
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "guard_error"
    outbox.remove_claim(name)


def test_capture_socket_is_not_regular(outbox):
    # A UNIX socket: lstat gate -> not_regular. (open() would ENXIO, not ELOOP —
    # errno-matching on the open alone would mis-map it to guard_error.)
    sp = os.path.join(outbox._root_realpath, "sock.pdf")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(sp)
    try:
        name = outbox.claim(sp)
        with pytest.raises(OutboxError) as ei:
            outbox.capture(name, "document")
        assert ei.value.kind == "not_regular"
        outbox.remove_claim(name)
    finally:
        srv.close()


def test_capture_one_byte_magic_mismatch(outbox):
    name = _claim_of(outbox, "one.pdf", b"x")
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "magic_mismatch"
    outbox.remove_claim(name)


def test_capture_non_eloop_open_failure_is_guard_error(outbox, monkeypatch):
    name = _claim_of(outbox, "g.pdf", PDF)
    real_open = os.open

    def fake_open(path, *a, **k):
        if path == name:                    # only the claim open fails; delegate the rest
            raise OSError(errno.EACCES, "denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr(plugin_outbox.os, "open", fake_open)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "guard_error"   # EACCES (not ELOOP) -> guard_error
    monkeypatch.undo()
    outbox.remove_claim(name)


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def _set_mtime(path, epoch_s):
    os.utime(path, (epoch_s, epoch_s))


def test_sweep_reaps_old_outbox_files_keeps_fresh(outbox):
    now_ms = 10_000_000_000_000
    old = _write_outbox_file(outbox._root_realpath, "old.pdf", PDF)
    fresh = _write_outbox_file(outbox._root_realpath, "fresh.pdf", PDF)
    _set_mtime(old, now_ms / 1000 - (MAX_AGE_S + 60))      # 2h+ old
    _set_mtime(fresh, now_ms / 1000 - 60)                   # 1 min old
    n = outbox.sweep_once(now_ms)
    assert n == 1
    assert not os.path.exists(old)
    assert os.path.exists(fresh)


def test_sweep_excludes_claims_dir_itself(outbox):
    now_ms = 10_000_000_000_000
    _set_mtime(outbox._claims_realpath, now_ms / 1000 - (MAX_AGE_S * 100))
    outbox.sweep_once(now_ms)
    assert os.path.isdir(outbox._claims_realpath)


def test_sweep_reaps_old_claims_by_embedded_epoch(outbox):
    now_ms = 10_000_000_000_000
    old_epoch = now_ms - (MAX_AGE_S + 120) * 1000
    fresh_epoch = now_ms - 30_000
    old_name = f"{old_epoch}-{'a' * 32}"
    fresh_name = f"{fresh_epoch}-{'b' * 32}"
    for nm in (old_name, fresh_name):
        with open(os.path.join(outbox._claims_realpath, nm), "wb") as fh:
            fh.write(b"z")
    # Give the old claim a RECENT mtime — rename preserves source mtime, so age
    # must come from the embedded epoch, NOT mtime.
    _set_mtime(os.path.join(outbox._claims_realpath, old_name), now_ms / 1000 - 10)
    n = outbox.sweep_once(now_ms)
    assert not os.path.exists(os.path.join(outbox._claims_realpath, old_name))
    assert os.path.exists(os.path.join(outbox._claims_realpath, fresh_name))
    assert n == 1


def test_sweep_reaps_directory_typed_stale_claim(outbox):
    now_ms = 10_000_000_000_000
    old_epoch = now_ms - (MAX_AGE_S + 120) * 1000
    d = os.path.join(outbox._claims_realpath, f"{old_epoch}-{'c' * 32}")
    os.mkdir(d)
    with open(os.path.join(d, "inner"), "wb") as fh:
        fh.write(b"z")
    outbox.sweep_once(now_ms)
    assert not os.path.exists(d)


async def test_sweep_job_reaps_and_runs_off_loop(tmp_path, monkeypatch):
    """The casa_core sweep coroutine reaps orphans AND runs the reap off the
    event loop (worker thread) — asserted by a differing thread ident."""
    import threading
    ob = plugin_outbox.init_outbox(str(tmp_path / "job-ob"))
    try:
        old = _write_outbox_file(ob._root_realpath, "old.pdf", PDF)
        _set_mtime(old, os.stat(old).st_mtime - (MAX_AGE_S + 60))
        main_ident = threading.get_ident()
        seen: dict = {}
        real_sweep = ob.sweep_once

        def spy(now_ms):
            seen["ident"] = threading.get_ident()
            return real_sweep(now_ms)

        monkeypatch.setattr(ob, "sweep_once", spy)     # sweep_now -> sweep_once
        n = await plugin_outbox.sweep_job()
        assert n == 1
        assert not os.path.exists(old)
        assert seen["ident"] != main_ident             # ran off the event loop
    finally:
        ob.close()
        plugin_outbox._OUTBOX = None


async def test_sweep_job_noop_when_uninitialised(monkeypatch):
    monkeypatch.setattr(plugin_outbox, "_OUTBOX", None)
    assert await plugin_outbox.sweep_job() == 0


async def test_wire_inits_and_registers_hourly_job(tmp_path):
    """Executable wiring coverage: wire() inits the outbox AND registers the
    hourly job on a fake scheduler — catches a misregistered trigger/id that
    byte-compiling casa_core cannot."""
    jobs: list = []

    class _FakeScheduler:
        def add_job(self, func, **kw):
            jobs.append((func, kw))

    await plugin_outbox.wire(_FakeScheduler(), str(tmp_path / "wired-ob"))
    try:
        assert plugin_outbox.get_outbox() is not None       # init happened
        assert len(jobs) == 1
        func, kw = jobs[0]
        assert func is plugin_outbox.sweep_job
        assert kw["id"] == "plugin_outbox_sweep"
        assert kw["trigger"] == "interval" and kw["hours"] == 1
        assert kw["max_instances"] == 1
    finally:
        plugin_outbox.get_outbox().close()
        plugin_outbox._OUTBOX = None
