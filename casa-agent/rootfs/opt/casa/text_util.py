"""Small text utilities shared across Casa.

Currently houses ``truncate_for_topic``, used by tools.py to fit
Telegram forum-topic names within the Bot API's ~128-byte limit
without slicing mid-word (E-9 from bug-review-2026-04-29).
"""

from __future__ import annotations

# Telegram Bot API limit for createForumTopic 'name' parameter, in
# bytes. (Documented as ~128; tested empirically at 128.)
TELEGRAM_TOPIC_NAME_BYTES = 128

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
