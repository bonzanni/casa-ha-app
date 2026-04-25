"""Bug 11 (v0.14.6): drivers.claude_code_driver._tail_file must follow
file rotation (s6-log rename-replace) and truncate-in-place.

Pre-fix: the loop tracked only `pos` (byte offset). After s6-log moved
`current` aside and started a fresh small file at the same path, the
seek to the old (large) pos landed past EOF of the new file and
readline returned "" forever, silently dropping all logs below the
prior cutoff.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def _start_tail_into_queue(path: Path) -> tuple[asyncio.Task, asyncio.Queue]:
    """Run _tail_file in a background task, push every line into a queue.

    Using a long-lived task keeps the async generator alive across the
    test's sleeps and file mutations. Calling asyncio.wait_for on
    gen.__anext__() would cancel the generator on timeout, terminating
    the tail loop — which makes restart-after-rotation impossible to test.
    """
    from drivers.claude_code_driver import _tail_file
    q: asyncio.Queue = asyncio.Queue()

    async def runner() -> None:
        async for line in _tail_file(str(path)):
            await q.put(line)

    task = asyncio.create_task(runner())
    return task, q


async def _drain_for(q: asyncio.Queue, duration: float) -> list[str]:
    """Wait `duration` seconds, then return everything currently in the queue."""
    await asyncio.sleep(duration)
    out: list[str] = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass


async def test_yields_existing_lines(tmp_path: Path) -> None:
    path = tmp_path / "current"
    path.write_text("a\nb\nc\n", encoding="utf-8")

    task, q = await _start_tail_into_queue(path)
    try:
        lines = await _drain_for(q, duration=0.4)
        assert lines == ["a\n", "b\n", "c\n"]
    finally:
        await _stop(task)


async def test_appended_lines_yielded(tmp_path: Path) -> None:
    path = tmp_path / "current"
    path.write_text("first\n", encoding="utf-8")

    task, q = await _start_tail_into_queue(path)
    try:
        await asyncio.sleep(0.3)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("second\n")
        lines = await _drain_for(q, duration=0.5)
        assert lines == ["first\n", "second\n"]
    finally:
        await _stop(task)


async def test_rotation_via_replace_resumes_at_zero(tmp_path: Path) -> None:
    """s6-log rotation: rename current -> archive, write new current.

    Pre-fix bug: pos stayed at the old (large) end-offset, so the new
    smaller current was invisible until it grew past that offset.
    """
    path = tmp_path / "current"
    archive = tmp_path / "@archive.s"

    # Old file: enough bytes that the post-rotation pos points way past
    # the start of the replacement file.
    big = "x" * 4096 + "\n"
    path.write_text(big + "old-line\n", encoding="utf-8")

    task, q = await _start_tail_into_queue(path)
    try:
        # Let the tail consume the existing content + advance pos to EOF.
        await asyncio.sleep(0.4)

        # Rotate: move current aside, write a NEW small current.
        os.replace(path, archive)
        path.write_text("new-line-1\nnew-line-2\n", encoding="utf-8")

        await asyncio.sleep(0.5)
        # Pull whatever is in the queue. The new lines MUST appear.
        all_lines: list[str] = []
        while True:
            try:
                all_lines.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break

        assert "new-line-1\n" in all_lines, (
            f"rotation lost new-line-1 (Bug 11). all queued: {all_lines!r}"
        )
        assert "new-line-2\n" in all_lines, (
            f"rotation lost new-line-2 (Bug 11). all queued: {all_lines!r}"
        )
    finally:
        await _stop(task)


async def test_truncate_in_place_resets_pos(tmp_path: Path) -> None:
    """File truncated below pos: subsequent reads see the new content."""
    path = tmp_path / "current"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    task, q = await _start_tail_into_queue(path)
    try:
        await asyncio.sleep(0.3)
        # Truncate in place to a single short line.
        path.write_text("z\n", encoding="utf-8")
        await asyncio.sleep(0.5)
        all_lines: list[str] = []
        while True:
            try:
                all_lines.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        assert "z\n" in all_lines, (
            f"truncate-in-place dropped 'z' line. queued: {all_lines!r}"
        )
    finally:
        await _stop(task)


async def test_missing_path_keeps_polling(tmp_path: Path) -> None:
    """Path not yet existing: tail keeps polling, picks up lines once it does."""
    path = tmp_path / "current"

    task, q = await _start_tail_into_queue(path)
    try:
        await asyncio.sleep(0.3)
        assert q.empty()
        path.write_text("hello\n", encoding="utf-8")
        await asyncio.sleep(0.5)
        lines = await _drain_for(q, duration=0.0)
        assert lines == ["hello\n"]
    finally:
        await _stop(task)
