"""Unit tests for the retry helpers (spec 5.2 §3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from retry import (
    RETRY_KINDS,
    compute_backoff_ms,
    parse_retry_after_ms,
    retry_sdk_call,
)
from agent import ErrorKind


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# compute_backoff_ms
# ---------------------------------------------------------------------------


class TestComputeBackoffMs:
    def test_first_attempt_is_initial(self):
        # Jitter is multiplicative 0.5–1.0x of the ceiling, so the minimum
        # for attempt 0 equals half the initial.
        vals = [
            compute_backoff_ms(0, initial_ms=500, cap_ms=8000)
            for _ in range(100)
        ]
        assert all(250 <= v <= 500 for v in vals), vals

    def test_exponential_growth_until_cap(self):
        # attempt 0 → 500, attempt 1 → 1000, attempt 2 → 2000, attempt 3 → 4000,
        # attempt 4 → 8000 (cap), attempt 5 → 8000 (stays capped).
        def ceiling(a):
            return min(500 * (2 ** a), 8000)

        for attempt in range(6):
            v = compute_backoff_ms(attempt, initial_ms=500, cap_ms=8000)
            c = ceiling(attempt)
            assert c / 2 <= v <= c, (attempt, v, c)

    def test_cap_applies_for_large_attempt_numbers(self):
        # attempt 20 with 500ms initial would be 500 * 2**20 without cap;
        # cap forces everything ≤ cap_ms.
        v = compute_backoff_ms(20, initial_ms=500, cap_ms=8000)
        assert 4000 <= v <= 8000


# ---------------------------------------------------------------------------
# parse_retry_after_ms
# ---------------------------------------------------------------------------


class TestParseRetryAfterMs:
    def test_plain_seconds(self):
        exc = RuntimeError("429 rate limit, retry-after: 5")
        assert parse_retry_after_ms(exc) == 5000

    def test_retry_after_key_value(self):
        exc = RuntimeError("Rate limited. Retry-After: 12s")
        assert parse_retry_after_ms(exc) == 12000

    def test_fractional_seconds(self):
        exc = RuntimeError("retry-after: 0.5")
        assert parse_retry_after_ms(exc) == 500

    def test_absent_returns_none(self):
        exc = RuntimeError("just a generic 429")
        assert parse_retry_after_ms(exc) is None

    def test_non_numeric_returns_none(self):
        exc = RuntimeError("retry-after: soon")
        assert parse_retry_after_ms(exc) is None


# ---------------------------------------------------------------------------
# RETRY_KINDS
# ---------------------------------------------------------------------------


class TestRetryKinds:
    def test_retryable_set(self):
        assert RETRY_KINDS == frozenset({
            ErrorKind.TIMEOUT,
            ErrorKind.RATE_LIMIT,
            ErrorKind.SDK_ERROR,
        })

    def test_unknown_is_not_retryable(self):
        assert ErrorKind.UNKNOWN not in RETRY_KINDS

    def test_memory_error_is_not_retryable(self):
        assert ErrorKind.MEMORY_ERROR not in RETRY_KINDS

    def test_channel_error_is_not_retryable(self):
        assert ErrorKind.CHANNEL_ERROR not in RETRY_KINDS
