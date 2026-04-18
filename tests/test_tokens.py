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


# ---------------------------------------------------------------------------
# extract_usage
# ---------------------------------------------------------------------------


class TestExtractUsage:
    """Spec §5.2: ResultMessage.usage carries input / output / cache_read /
    cache_write. Real SDK shape may be a dataclass with attributes; mock
    SDK uses a dict. extract_usage must handle both."""

    def test_attribute_style_result_message(self):
        class FakeResult:
            usage = {
                "input_tokens": 1203,
                "output_tokens": 82,
                "cache_read_input_tokens": 8021,
                "cache_creation_input_tokens": 0,
            }
        out = extract_usage(FakeResult())
        assert out["input_tokens"] == 1203
        assert out["output_tokens"] == 82
        assert out["cache_read_input_tokens"] == 8021
        assert out["cache_creation_input_tokens"] == 0

    def test_missing_usage_attribute_returns_zeros(self):
        class FakeResult:
            pass  # no .usage at all
        out = extract_usage(FakeResult())
        assert out == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    def test_partial_usage_fills_zeros_for_missing_keys(self):
        class FakeResult:
            usage = {"input_tokens": 10}
        out = extract_usage(FakeResult())
        assert out["input_tokens"] == 10
        assert out["output_tokens"] == 0
        assert out["cache_read_input_tokens"] == 0
        assert out["cache_creation_input_tokens"] == 0

    def test_none_usage_returns_zeros(self):
        # Defensive: SDK could emit usage=None on cancellation.
        class FakeResult:
            usage = None
        out = extract_usage(FakeResult())
        assert all(v == 0 for v in out.values())

    def test_string_values_coerced_to_int(self):
        # Defensive: a careless SDK could string-encode the counts.
        class FakeResult:
            usage = {"input_tokens": "42", "output_tokens": "0"}
        out = extract_usage(FakeResult())
        assert out["input_tokens"] == 42
        assert out["output_tokens"] == 0
