"""Per-key inbound token-bucket rate limiting (spec 5.2 §8).

Each `RateLimiter` owns a dict of `TokenBucket`s keyed on an arbitrary
string. On every ingress, the channel calls ``limiter.check(key)`` which
returns a :class:`RateDecision` with three fields:

- ``allowed``: whether to admit the request;
- ``should_notify``: True on the first reject after any allow (Telegram
  uses this so it only sends ONE "slow down" reply per reject streak);
- ``retry_after_s``: suggested backoff in seconds (rounded up to an
  integer by :func:`rate_limit_response` for the `Retry-After` header).

Capacity ``0`` disables the limit entirely — :meth:`RateLimiter.check`
short-circuits without even creating a bucket, so disabled limiters
don't grow a per-key dict proportional to the number of unique callers
seen. Matches spec §8.3: "Zero disables the limit for that channel."

Pure policy module. Imports stdlib only plus aiohttp (already a Casa
dep) for the webhook `Response` helper. No imports from bus, channels,
agent, or memory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from aiohttp import web


@dataclass(frozen=True)
class RateDecision:
    """Result of one rate-limit check."""

    allowed: bool
    should_notify: bool
    retry_after_s: float


class TokenBucket:
    """Single-key token bucket with an injectable clock.

    Thread-unsafe; intended for use on Casa's single asyncio event loop.
    The refill + decrement pass inside :meth:`check` has no await
    points so concurrent dispatch tasks land in deterministic order.
    """

    __slots__ = (
        "_capacity", "_refill_per_s", "_tokens", "_last", "_notified", "_now",
    )

    def __init__(
        self,
        capacity: int,
        refill_per_s: float,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._capacity = capacity
        self._refill_per_s = max(0.0, refill_per_s)
        self._now = now or time.monotonic
        self._tokens: float = float(max(0, capacity))
        self._last: float = self._now()
        self._notified: bool = False

    def check(self) -> RateDecision:
        if self._capacity <= 0:
            return RateDecision(True, False, 0.0)

        now = self._now()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(
            float(self._capacity),
            self._tokens + elapsed * self._refill_per_s,
        )

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            self._notified = False
            return RateDecision(True, False, 0.0)

        deficit = 1.0 - self._tokens
        retry_after = (
            deficit / self._refill_per_s if self._refill_per_s > 0 else 60.0
        )
        should_notify = not self._notified
        self._notified = True
        return RateDecision(False, should_notify, retry_after)


class RateLimiter:
    """Keyed token-bucket limiter; lazily creates one bucket per key.

    ``capacity`` is both the bucket size (max burst) and the steady-state
    count per ``window_s``. A fresh bucket starts full, so the very first
    burst of ``capacity`` calls is admitted instantly.

    ``capacity == 0`` disables the limit: every :meth:`check` returns
    ``allowed=True`` and — critically — no bucket is instantiated, so
    the internal dict does not grow with the number of unique keys.
    """

    def __init__(
        self,
        *,
        capacity: int,
        window_s: float = 60.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._capacity = capacity
        self._window_s = window_s
        self._refill_per_s = (
            capacity / window_s if (capacity > 0 and window_s > 0) else 0.0
        )
        self._now = now
        self._buckets: dict[str, TokenBucket] = {}

    @property
    def enabled(self) -> bool:
        return self._capacity > 0

    def check(self, key: str) -> RateDecision:
        if not self.enabled:
            return RateDecision(True, False, 0.0)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(
                self._capacity, self._refill_per_s, now=self._now,
            )
            self._buckets[key] = bucket
        return bucket.check()


def rate_limit_response(
    limiter: RateLimiter, key: str,
) -> web.Response | None:
    """aiohttp helper: return a 429 response on reject, else ``None``.

    Used by the webhook + invoke handlers in ``casa_core``. The
    ``Retry-After`` header is integer seconds rounded *up* from the
    underlying bucket's ``retry_after_s`` (so the client never polls
    before a token has actually refilled). Per RFC 7231 §7.1.3 the
    header may also be an HTTP-date; seconds is simpler and equally
    valid.
    """
    decision = limiter.check(key)
    if decision.allowed:
        return None
    retry_after = max(1, int(decision.retry_after_s) + 1)
    return web.json_response(
        {"error": "rate_limited"},
        status=429,
        headers={"Retry-After": str(retry_after)},
    )
