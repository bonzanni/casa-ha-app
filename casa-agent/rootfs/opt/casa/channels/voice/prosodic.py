"""Delta-fed, tag-opaque prosodic splitter (spec §5.1).

Feed token suffixes via ``feed(delta)`` — it returns any blocks that
closed on this delta. Call ``flush_tail()`` on stream end to drain the
buffer.
"""

from __future__ import annotations

import time

_SENTENCE_MARKS = ".!?…"
_CLAUSE_MARKS = ",;"
_OPEN = {"[": "]", "(": ")", "{": "}", "<": ">"}
_CHAR_CAP = 200
_TIME_CAP = 1.5  # seconds since last flush


class ProsodicSplitter:
    def __init__(self) -> None:
        self._buf: str = ""
        self._last_flush: float = time.monotonic()

    # --- public -------------------------------------------------------

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        return self._drain()

    def flush_tail(self) -> str:
        if not self._buf:
            return ""
        out, self._buf = self._buf, ""
        self._last_flush = time.monotonic()
        return out

    # --- internal -----------------------------------------------------

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            cut = self._find_cut(self._buf)
            if cut is None:
                break
            block, self._buf = self._buf[:cut], self._buf[cut:].lstrip()
            out.append(block.rstrip())
            self._last_flush = time.monotonic()
        # Safety caps
        cap_block = self._safety_cap()
        if cap_block is not None:
            out.append(cap_block)
        return out

    def _find_cut(self, s: str) -> int | None:
        """Index *after* the sentence mark (or paragraph break) that
        ends the first complete block in *s*, or None if the buffer is
        not yet flushable.
        """
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch in _OPEN:
                close = self._match_close(s, i)
                if close is None:
                    return None  # unclosed bracket — wait
                i = close + 1
                continue
            if ch == "\n" and i + 1 < n and s[i + 1] == "\n":
                return i  # paragraph break
            if ch in _SENTENCE_MARKS:
                return i + 1
            i += 1
        return None

    @staticmethod
    def _match_close(s: str, open_idx: int) -> int | None:
        close_char = _OPEN[s[open_idx]]
        depth = 1
        for j in range(open_idx + 1, len(s)):
            c = s[j]
            if c == s[open_idx]:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return j
        return None

    def _safety_cap(self) -> str | None:
        """If the buffer has blown the char/time cap, force a break."""
        now = time.monotonic()
        too_long = len(self._buf) >= _CHAR_CAP
        too_slow = (now - self._last_flush) >= _TIME_CAP and self._buf
        if not (too_long or too_slow):
            return None

        window = self._buf[:_CHAR_CAP] if too_long else self._buf
        cut = self._rightmost_clause_mark(window)
        if cut is None:
            cut = len(window)  # hard cut
        block, self._buf = self._buf[:cut], self._buf[cut:].lstrip()
        self._last_flush = now
        return block.rstrip()

    @staticmethod
    def _rightmost_clause_mark(s: str) -> int | None:
        """Return index AFTER rightmost clause mark outside any bracket, or None."""
        depth = 0
        open_stack: list[str] = []
        last: int | None = None
        i = 0
        while i < len(s):
            c = s[i]
            if c in _OPEN:
                open_stack.append(_OPEN[c])
                depth += 1
            elif open_stack and c == open_stack[-1]:
                open_stack.pop()
                depth -= 1
            elif depth == 0 and c in _CLAUSE_MARKS:
                last = i + 1
            i += 1
        return last
