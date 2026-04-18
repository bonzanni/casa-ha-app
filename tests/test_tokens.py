"""Unit tests for the token-accounting helpers (spec 5.2 §5)."""

from __future__ import annotations

import logging

import pytest

from tokens import (
    BudgetTracker,
    estimate_tokens,
    extract_usage,
    format_turn_summary,
)


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_chars_divided_by_four(self):
        assert estimate_tokens("abcd" * 1000) == 1000

    def test_short_text_rounds_down(self):
        # 7 chars // 4 == 1 (floor).
        assert estimate_tokens("abcdefg") == 1

    def test_empty_returns_zero(self):
        assert estimate_tokens("") == 0

    def test_none_returns_zero(self):
        # Defensive: memory_context can be empty/missing in early turns.
        assert estimate_tokens(None) == 0  # type: ignore[arg-type]

    def test_unicode_uses_char_count_not_byte_count(self):
        # 4 multibyte chars → 1 token by len(), not by encode("utf-8").
        assert estimate_tokens("üöäß") == 1
