"""Tests for casa.text_util.truncate_for_topic.

Telegram's createForumTopic name parameter is limited to ~128 bytes
(per Bot API). The helper must respect this BYTE budget (not char),
break on word boundaries when possible, and signal truncation with
a Unicode ellipsis '…' (3 UTF-8 bytes).
"""

from __future__ import annotations

import pytest

from text_util import truncate_for_topic


def test_truncate_short_text_passes_through():
    out = truncate_for_topic("hello", byte_budget=128)
    assert out == "hello"


def test_truncate_word_boundary_with_ellipsis():
    text = "Add a one-line personality trait to Ellen's agent config"
    # Budget chosen so we cut mid-sentence on a word boundary.
    out = truncate_for_topic(text, byte_budget=30)
    assert out.endswith("…")
    # Must not slice mid-word: trailing char before '…' is whitespace
    # ABSENT (we strip), and the chars that ARE there form a complete
    # prefix of the original up to a word boundary.
    body = out[:-1]  # drop the '…'
    assert text.startswith(body)
    # The break point must coincide with a space in the original.
    assert text[len(body)] == " " or len(body) == len(text)
    # And total bytes must fit budget.
    assert len(out.encode("utf-8")) <= 30


def test_truncate_strips_trailing_punctuation():
    """Word-boundary truncation must not leave dangling ', ' or '. '
    before the ellipsis."""
    text = "Hello, world. Sample task that goes on and on and on"
    # Budget chosen so the natural word break would leave ", " or
    # ". " just before the '…'. The helper must strip the punctuation.
    out = truncate_for_topic(text, byte_budget=20)
    assert out.endswith("…")
    # Char immediately before '…' must NOT be ',', ';', ':', '.', or ' '.
    pre = out[:-1].rstrip()
    assert pre and pre[-1] not in ",;:."


def test_truncate_byte_budget_emoji():
    """Multi-byte characters count by UTF-8 byte length, not Python
    char length."""
    # Each '🎉' is 4 UTF-8 bytes. With a 12-byte budget we can fit
    # at most 2 emoji (8 bytes) + '…' (3 bytes) = 11 bytes.
    text = "🎉🎉🎉🎉 task"
    out = truncate_for_topic(text, byte_budget=12)
    assert len(out.encode("utf-8")) <= 12


def test_truncate_pathological_short_budget():
    # No room for the 3-byte '…' → empty string.
    assert truncate_for_topic("hello world", byte_budget=2) == ""
    assert truncate_for_topic("hello world", byte_budget=0) == ""
    # Exactly 3 bytes fits only the ellipsis when truncation is needed.
    out = truncate_for_topic("hello world", byte_budget=3)
    assert out == "…"


def test_truncate_single_long_word():
    """A single word longer than budget: hard byte-cut + '…'."""
    text = "supercalifragilisticexpialidocious"  # 34 ASCII bytes
    out = truncate_for_topic(text, byte_budget=10)
    assert out.endswith("…")
    assert len(out.encode("utf-8")) <= 10
    # First chars must be a prefix of the original word.
    assert text.startswith(out[:-1])
