"""Small text utilities shared across Casa.

Houses ``truncate_for_topic``, used by tools.py to fit Telegram
forum-topic names within the Bot API's ~128-byte limit without
slicing mid-word (E-9 from bug-review-2026-04-29); ``sanitize_segment``,
the documented Claude Code plugin MCP-tool-name namespace sanitization
(re-exported by ``plugin_grants`` for its existing callers/tests); and
``is_unsafe_text``, the pinned UNSAFE-TEXT codepoint predicate (v0.78.0
design doc, W1/W2) reused verbatim by ``plugin_store.manifest_protected_tools``
for protectedTools summary templates (W1) and by the W2 challenge
renderer for interpolated values and display names.

STDLIB-ONLY (no third-party imports): ``plugin_store.py`` is copied into
the image and imported by the Dockerfile build helper BEFORE any venv
exists, so anything it imports — including this module — must stay
stdlib-only.
"""

from __future__ import annotations

import re

# Telegram Bot API limit for createForumTopic 'name' parameter, in
# bytes. (Documented as ~128; tested empirically at 128.)
TELEGRAM_TOPIC_NAME_BYTES = 128

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_segment(s: str) -> str:
    """Documented CC sanitization for MCP-tool namespace segments: any char
    outside ``A-Za-z0-9_-`` becomes ``_``."""
    return _SANITIZE_RE.sub("_", s)


# UNSAFE-TEXT predicate (pinned, 2026-07-14 approval-summaries design
# section W1/W2): text is UNSAFE iff it contains any codepoint in
# U+0000-001F (C0, incl. newline/CR), U+007F-009F (DEL + C1),
# U+2028/U+2029 (line/paragraph separator), U+061C (Arabic Letter Mark),
# U+200E/U+200F (LRM/RLM), U+202A-202E (bidi embedding/override), or
# U+2066-2069 (bidi isolates). Single-line-ness is implied by the C0
# exclusion. Built from an explicit integer codepoint table (never a
# raw literal control/bidi glyph in source) so the pinned ranges stay
# auditable.
_UNSAFE_TEXT_RANGES = (
    (0x0000, 0x001F),
    (0x007F, 0x009F),
    (0x2028, 0x2028),
    (0x2029, 0x2029),
    (0x061C, 0x061C),
    (0x200E, 0x200F),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
)
_UNSAFE_TEXT_RE = re.compile(
    "[" + "".join(chr(lo) + "-" + chr(hi) for lo, hi in _UNSAFE_TEXT_RANGES) + "]"
)


def is_unsafe_text(s: str) -> bool:
    """True iff ``s`` contains any UNSAFE-TEXT codepoint (see module docstring
    and the regex comment above). Reused verbatim for protectedTools summary
    templates (W1), and — unmodified by this change — intended for W2's
    interpolated values and display names, so all three call sites can never
    drift apart."""
    return bool(_UNSAFE_TEXT_RE.search(s))


_ELLIPSIS = "…"
_ELLIPSIS_BYTES = len(_ELLIPSIS.encode("utf-8"))  # 3
_TRAILING_PUNCT = ",;:."


def truncate_for_topic(text: str, *, byte_budget: int) -> str:
    """Truncate ``text`` so its UTF-8 byte length is ≤ ``byte_budget``.

    Breaks on the last whitespace boundary when possible and signals
    truncation with a trailing Unicode ellipsis '…' (3 UTF-8 bytes).
    Strips trailing punctuation in {',;:.'} before the ellipsis to
    avoid orphan punctuation. The returned string's UTF-8 byte length
    is *strictly* ≤ byte_budget.

    Edge cases:
    - empty ``text`` → empty string.
    - ``text`` already fits → returned unchanged.
    - ``byte_budget`` < 3 (cannot fit '…') → empty string.
    - ``byte_budget`` ≥ 3 but no whitespace boundary fits → hard
      byte-cut on the last UTF-8 boundary that fits within
      ``byte_budget - 3`` bytes, then append '…'.
    """
    if not text:
        return ""

    raw = text.encode("utf-8")
    if len(raw) <= byte_budget:
        return text

    if byte_budget < _ELLIPSIS_BYTES:
        return ""

    # Budget for the body (everything before '…').
    body_byte_budget = byte_budget - _ELLIPSIS_BYTES

    # Walk text char-by-char, accumulating bytes, tracking the last
    # whitespace boundary as a candidate break point.
    body: list[str] = []
    body_bytes = 0
    last_space_idx_in_body = -1  # index into body[] just AFTER a space
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if body_bytes + ch_bytes > body_byte_budget:
            break
        body.append(ch)
        body_bytes += ch_bytes
        if ch == " ":
            last_space_idx_in_body = len(body)  # break AT the space

    if not body:
        # Even the first char doesn't fit; return just the ellipsis.
        return _ELLIPSIS

    # Prefer to break on the last whitespace if we found one; else
    # hard-cut.
    if last_space_idx_in_body > 0:
        truncated = "".join(body[:last_space_idx_in_body])
    else:
        truncated = "".join(body)

    # Strip trailing whitespace and orphan punctuation.
    truncated = truncated.rstrip()
    while truncated and truncated[-1] in _TRAILING_PUNCT:
        truncated = truncated[:-1].rstrip()

    return truncated + _ELLIPSIS
