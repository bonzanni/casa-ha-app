"""Tests for the canonical Honcho session-id builder.

Background: every Casa Honcho write since v0.2.2 returned 422 because
session ids contained ``:``, which Honcho's server-side
``^[A-Za-z0-9_-]+$`` resource-name regex rejects. ``honcho_session_id``
is the single source of truth for building compliant ids.
"""

import re

import pytest

from honcho_ids import honcho_session_id

# Must mirror the Honcho server pattern from
# upstream src/schemas/api.py:37 (plastic-labs/honcho).
HONCHO_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def test_two_segments():
    assert honcho_session_id("finance", "nicola") == "finance-nicola"


def test_three_segments():
    assert (
        honcho_session_id("telegram-1234", "domestic", "assistant")
        == "telegram-1234-domestic-assistant"
    )


def test_four_segments():
    assert (
        honcho_session_id("voice", "kitchen-sat", "house", "butler")
        == "voice-kitchen-sat-house-butler"
    )


def test_output_matches_honcho_pattern():
    sid = honcho_session_id("telegram", "1234", "meta", "assistant")
    assert HONCHO_PATTERN.fullmatch(sid)


def test_numeric_chat_id_str_coerced_by_caller():
    """The builder rejects non-str; callers must `str(int)` themselves."""
    assert honcho_session_id("telegram", str(1234), "meta", "assistant") == (
        "telegram-1234-meta-assistant"
    )


def test_negative_chat_id_string_works():
    """Telegram group chat ids are negative; ``-`` is regex-clean."""
    sid = honcho_session_id("telegram", str(-1001234567890), "house", "butler")
    assert sid == "telegram--1001234567890-house-butler"
    assert HONCHO_PATTERN.fullmatch(sid)


def test_underscore_in_part_allowed():
    sid = honcho_session_id("telegram", "1", "meta", "butler_v2")
    assert sid == "telegram-1-meta-butler_v2"
    assert HONCHO_PATTERN.fullmatch(sid)


def test_reject_zero_parts():
    with pytest.raises(ValueError, match="at least one part"):
        honcho_session_id()


def test_reject_empty_part():
    with pytest.raises(ValueError, match="part 1 is empty"):
        honcho_session_id("telegram", "")


def test_reject_non_str_part():
    with pytest.raises(ValueError, match="part 1 must be str, got int"):
        honcho_session_id("telegram", 1234)  # type: ignore[arg-type]


def test_reject_colon_in_part():
    """The bug we're fixing: a colon-joined string passed as a single part.

    Catches the ``forgot one site`` regression — if someone copy-pastes
    ``f"{channel}:{chat_id}:meta:assistant"`` into the new builder as a
    single arg, the builder must refuse rather than emit an invalid id.
    """
    with pytest.raises(ValueError, match=r"part 0=.*outside \[A-Za-z0-9_-\]"):
        honcho_session_id("voice:probe-scope:house:butler")


def test_reject_whitespace_in_part():
    with pytest.raises(ValueError, match=r"outside \[A-Za-z0-9_-\]"):
        honcho_session_id("telegram", "with space", "meta", "assistant")


def test_reject_other_punct_in_part():
    with pytest.raises(ValueError, match=r"outside \[A-Za-z0-9_-\]"):
        honcho_session_id("telegram", "1", "meta", "butler.v2")


def test_reject_over_100_chars():
    long = "x" * 60
    with pytest.raises(ValueError, match="Honcho rejects > 100"):
        honcho_session_id(long, long)  # joined = 121 chars


def test_pre_fix_session_id_would_trip_honcho_regex():
    """Regression fixture — locks in why honcho_session_id exists.

    The pre-v0.17.1 shape ``f"{channel}:{chat_id}:{scope}:{role}"``
    matches NEITHER the new builder's per-part regex NOR the Honcho
    server's overall regex. This test passes the literal pre-fix
    string as a single part and confirms ``honcho_session_id`` rejects
    it — the same character class Honcho rejects upstream.

    If this test ever passes a colon-containing string, the fix has
    silently regressed.
    """
    pre_fix = "telegram:123456:domestic:assistant"
    assert ":" in pre_fix  # sanity
    with pytest.raises(ValueError, match=r"outside \[A-Za-z0-9_-\]"):
        honcho_session_id(pre_fix)

    # Independently: every individual segment IS clean — proving the
    # ONLY issue is the separator. The new builder joins on '-' so
    # the resulting id IS Honcho-compliant.
    sid = honcho_session_id("telegram", "123456", "domestic", "assistant")
    assert HONCHO_PATTERN.fullmatch(sid)
