"""Unit tests for rate_limit (spec 5.2 §8)."""

from __future__ import annotations

import pytest

from rate_limit import (
    RateDecision,
    RateLimiter,
    TokenBucket,
    rate_limit_response,
)


# ---------------------------------------------------------------------------
# TokenBucket — burst, refill, disabled, notify-once, retry-after
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock whose `now` the test drives manually."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestTokenBucket:
    def test_fresh_bucket_admits_full_burst(self):
        clock = _FakeClock()
        b = TokenBucket(capacity=5, refill_per_s=5 / 60.0, now=clock)
        for _ in range(5):
            d = b.check()
            assert d.allowed is True
            assert d.should_notify is False
            assert d.retry_after_s == 0.0

    def test_reject_after_burst_exceeds_capacity(self):
        clock = _FakeClock()
        b = TokenBucket(capacity=3, refill_per_s=3 / 60.0, now=clock)
        for _ in range(3):
            assert b.check().allowed is True
        d = b.check()
        assert d.allowed is False
        assert d.should_notify is True
        # 1 / (3/60) = 20 s until the next whole token.
        assert 15.0 <= d.retry_after_s <= 25.0

    def test_notify_fires_only_on_first_reject_of_streak(self):
        clock = _FakeClock()
        b = TokenBucket(capacity=2, refill_per_s=2 / 60.0, now=clock)
        # Exhaust the bucket.
        assert b.check().allowed is True
        assert b.check().allowed is True
        # First reject: notify.
        d1 = b.check()
        assert d1.allowed is False and d1.should_notify is True
        # Subsequent rejects in the same streak: no notify.
        for _ in range(5):
            d = b.check()
            assert d.allowed is False and d.should_notify is False
        # Advance time far enough to refill one token.
        clock.advance(60.0)
        d2 = b.check()
        assert d2.allowed is True and d2.should_notify is False
        # Exhaust to zero then reject again — notify flag reset.
        d3 = b.check()
        # After one admitted call tokens ≈ 0; next call likely rejects.
        if d3.allowed:
            d3 = b.check()
        assert d3.allowed is False and d3.should_notify is True, (
            "notify flag must reset once the bucket admits a request again"
        )

    def test_refill_crosses_reject_back_to_allow(self):
        clock = _FakeClock()
        b = TokenBucket(capacity=1, refill_per_s=1 / 60.0, now=clock)
        assert b.check().allowed is True
        assert b.check().allowed is False
        # Exactly one window later, one token has refilled.
        clock.advance(60.0)
        assert b.check().allowed is True

    def test_refill_caps_at_capacity(self):
        clock = _FakeClock()
        b = TokenBucket(capacity=3, refill_per_s=3 / 60.0, now=clock)
        # Exhaust, then sit idle for a very long time.
        for _ in range(3):
            b.check()
        clock.advance(3600.0)
        # Bucket can only hold `capacity` tokens; next 3 allow, 4th rejects.
        for _ in range(3):
            assert b.check().allowed is True
        assert b.check().allowed is False

    def test_capacity_zero_disables(self):
        clock = _FakeClock()
        b = TokenBucket(capacity=0, refill_per_s=0.0, now=clock)
        for _ in range(1000):
            d = b.check()
            assert d.allowed is True
            assert d.should_notify is False
            assert d.retry_after_s == 0.0

    def test_negative_capacity_disables_like_zero(self):
        """Defensive: a bad env value shouldn't crash the loop."""
        clock = _FakeClock()
        b = TokenBucket(capacity=-1, refill_per_s=0.0, now=clock)
        assert b.check().allowed is True

    def test_retry_after_falls_to_zero_as_bucket_fills(self):
        clock = _FakeClock()
        b = TokenBucket(capacity=2, refill_per_s=2 / 60.0, now=clock)
        b.check(); b.check()  # exhaust
        d1 = b.check()
        assert d1.retry_after_s > 0
        # Advance nearly a full window — should be close to zero.
        clock.advance(29.9)
        d2 = b.check()
        # Still rejected, but shrinking retry_after.
        assert d2.allowed is False
        assert d2.retry_after_s < d1.retry_after_s


# ---------------------------------------------------------------------------
# RateLimiter — per-key isolation, lazy creation, disabled short-circuit
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_per_key_isolation(self):
        clock = _FakeClock()
        limiter = RateLimiter(capacity=1, window_s=60.0, now=clock)
        assert limiter.check("chat-A").allowed is True
        # chat-A is now exhausted.
        assert limiter.check("chat-A").allowed is False
        # chat-B is a fresh bucket.
        assert limiter.check("chat-B").allowed is True

    def test_enabled_flag(self):
        assert RateLimiter(capacity=30).enabled is True
        assert RateLimiter(capacity=0).enabled is False
        assert RateLimiter(capacity=-1).enabled is False

    def test_disabled_short_circuits_without_growing_bucket_dict(self):
        limiter = RateLimiter(capacity=0)
        for i in range(1000):
            assert limiter.check(f"k{i}").allowed is True
        # The bucket dict must stay empty — disabled limiters shouldn't
        # leak memory proportional to unique keys seen.
        assert limiter._buckets == {}

    def test_bucket_created_on_first_check_then_reused(self):
        clock = _FakeClock()
        limiter = RateLimiter(capacity=5, window_s=60.0, now=clock)
        limiter.check("x")
        bucket_first = limiter._buckets["x"]
        limiter.check("x")
        bucket_second = limiter._buckets["x"]
        assert bucket_first is bucket_second

    def test_window_derives_refill_rate(self):
        """capacity=60 + window=60 → 1 token/sec refill."""
        clock = _FakeClock()
        limiter = RateLimiter(capacity=60, window_s=60.0, now=clock)
        for _ in range(60):
            assert limiter.check("g").allowed is True
        assert limiter.check("g").allowed is False
        clock.advance(1.0)
        assert limiter.check("g").allowed is True


# ---------------------------------------------------------------------------
# rate_limit_response helper (aiohttp)
# ---------------------------------------------------------------------------


class TestRateLimitResponse:
    def test_returns_none_when_allowed(self):
        limiter = RateLimiter(capacity=5, window_s=60.0)
        assert rate_limit_response(limiter, "k") is None

    def test_returns_429_with_retry_after_when_rejected(self):
        clock = _FakeClock()
        limiter = RateLimiter(capacity=1, window_s=60.0, now=clock)
        rate_limit_response(limiter, "k")  # consume the only token
        resp = rate_limit_response(limiter, "k")
        assert resp is not None
        assert resp.status == 429
        assert "Retry-After" in resp.headers
        # Integer seconds, >= 1, <= 60 (window size).
        value = int(resp.headers["Retry-After"])
        assert 1 <= value <= 61

    def test_disabled_limiter_never_returns_response(self):
        limiter = RateLimiter(capacity=0)
        for _ in range(100):
            assert rate_limit_response(limiter, "k") is None
