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

    def test_usage_as_object_with_attributes(self):
        # Defensive: real SDK could ship Usage as a dataclass with
        # attributes instead of a dict. The getattr-branch must work.
        class UsageObj:
            input_tokens = 50
            output_tokens = 10
            cache_read_input_tokens = 200
            cache_creation_input_tokens = 5
        class FakeResult:
            usage = UsageObj()
        out = extract_usage(FakeResult())
        assert out["input_tokens"] == 50
        assert out["output_tokens"] == 10
        assert out["cache_read_input_tokens"] == 200
        assert out["cache_creation_input_tokens"] == 5


# ---------------------------------------------------------------------------
# BudgetTracker
# ---------------------------------------------------------------------------


class TestBudgetTracker:
    """Spec §5.2: warn once per (session_id, role) when used > budget * 1.1
    for three turns in a row. Within-budget turns reset the streak."""

    def _records_for(self, caplog, session_id):
        return [r for r in caplog.records if session_id in r.getMessage()]

    def test_under_budget_never_warns(self, caplog):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        for _ in range(10):
            t.record("sess-A", used_tokens=100, budget=4000)
        assert self._records_for(caplog, "sess-A") == []

    def test_one_overrun_does_not_warn(self, caplog):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        # 4500 > 4000 * 1.1 = 4400 → counts as overrun, but only once.
        t.record("sess-A", used_tokens=4500, budget=4000)
        assert self._records_for(caplog, "sess-A") == []

    def test_two_consecutive_overruns_do_not_warn(self, caplog):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        t.record("sess-A", used_tokens=4500, budget=4000)
        t.record("sess-A", used_tokens=4500, budget=4000)
        assert self._records_for(caplog, "sess-A") == []

    def test_three_consecutive_overruns_warn_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        for _ in range(3):
            t.record("sess-A", used_tokens=4500, budget=4000)
        rows = self._records_for(caplog, "sess-A")
        assert len(rows) == 1
        msg = rows[0].getMessage()
        assert "4500" in msg
        assert "4000" in msg

    def test_warning_suppressed_for_subsequent_overruns_same_session(
        self, caplog,
    ):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        for _ in range(10):
            t.record("sess-A", used_tokens=5000, budget=4000)
        rows = self._records_for(caplog, "sess-A")
        assert len(rows) == 1, [r.getMessage() for r in rows]

    def test_under_budget_resets_streak(self, caplog):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        # Two overruns → under budget → two more overruns; streak never
        # reaches 3 → no warning.
        t.record("sess-A", used_tokens=4500, budget=4000)
        t.record("sess-A", used_tokens=4500, budget=4000)
        t.record("sess-A", used_tokens=100, budget=4000)
        t.record("sess-A", used_tokens=4500, budget=4000)
        t.record("sess-A", used_tokens=4500, budget=4000)
        assert self._records_for(caplog, "sess-A") == []

    def test_threshold_is_strict_greater_than_one_point_one_x(self, caplog):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        # Exactly 1.1× must NOT count as overrun (spec wording: 'exceeds').
        for _ in range(3):
            t.record("sess-A", used_tokens=4400, budget=4000)
        assert self._records_for(caplog, "sess-A") == []

    def test_separate_sessions_track_independently(self, caplog):
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        for _ in range(3):
            t.record("sess-A", used_tokens=5000, budget=4000)
            t.record("sess-B", used_tokens=5000, budget=4000)
        assert len(self._records_for(caplog, "sess-A")) == 1
        assert len(self._records_for(caplog, "sess-B")) == 1

    def test_zero_or_negative_budget_never_warns(self, caplog):
        # Defensive: a misconfigured agent (memory.token_budget=0) would
        # otherwise trip the warning on every turn forever.
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        for _ in range(10):
            t.record("sess-A", used_tokens=100, budget=0)
        assert self._records_for(caplog, "sess-A") == []

    def test_negative_budget_never_warns(self, caplog):
        # Defensive: a typo in memory.token_budget YAML producing -1
        # must not cascade into per-turn WARNINGS.
        caplog.set_level(logging.WARNING, logger="tokens")
        t = BudgetTracker()
        for _ in range(10):
            t.record("sess-A", used_tokens=100, budget=-1)
        assert self._records_for(caplog, "sess-A") == []


# ---------------------------------------------------------------------------
# format_turn_summary
# ---------------------------------------------------------------------------


class TestFormatTurnSummary:
    """One-line per-turn telemetry. No cost field (Max subscription —
    USD pricing would be theatre against list rates we don't pay)."""

    def test_canonical_line(self):
        usage = {
            "input_tokens": 1203,
            "output_tokens": 82,
            "cache_read_input_tokens": 8021,
            "cache_creation_input_tokens": 0,
        }
        line = format_turn_summary("butler", "voice", usage)
        assert line == (
            "turn_done role=butler channel=voice "
            "input=1203 output=82 cache_read=8021 cache_write=0"
        )

    def test_cache_fields_kept_separate(self):
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        }
        line = format_turn_summary("assistant", "telegram", usage)
        # Both kinds visible — cache_write signals first-population cost
        # for the next turn's prompt prefix.
        assert "cache_read=100" in line
        assert "cache_write=50" in line

    def test_missing_channel_renders_dash(self):
        # Agent._process passes ``msg.channel or "-"`` so an empty channel
        # lands as "-", matching the cid="-" convention from log_cid.
        usage = {
            "input_tokens": 1, "output_tokens": 1,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        line = format_turn_summary("assistant", "-", usage)
        assert "channel=-" in line

    def test_missing_usage_keys_render_zero(self):
        # extract_usage always fills the four keys, but be defensive.
        line = format_turn_summary("assistant", "telegram", {})
        assert line == (
            "turn_done role=assistant channel=telegram "
            "input=0 output=0 cache_read=0 cache_write=0"
        )
