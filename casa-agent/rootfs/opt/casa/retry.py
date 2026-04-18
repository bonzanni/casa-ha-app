"""Retry + backoff policy for the SDK call path (spec 5.2 §3).

Exposes a single public async coroutine :func:`retry_sdk_call` plus two
pure helpers used to compute the wait schedule and to extract a
server-supplied ``Retry-After`` hint from an exception message.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from typing import Awaitable, Callable, TypeVar

from agent import ErrorKind, _classify_error

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Config — env-driven, read at import time
# ---------------------------------------------------------------------------

MAX_ATTEMPTS: int = int(os.environ.get("SDK_RETRY_MAX_ATTEMPTS", "3"))
INITIAL_MS: int = int(os.environ.get("SDK_RETRY_INITIAL_MS", "500"))
CAP_MS: int = int(os.environ.get("SDK_RETRY_CAP_MS", "8000"))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

RETRY_KINDS: frozenset[ErrorKind] = frozenset({
    ErrorKind.TIMEOUT,
    ErrorKind.RATE_LIMIT,
    ErrorKind.SDK_ERROR,
})


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def compute_backoff_ms(
    attempt: int, *, initial_ms: int, cap_ms: int,
) -> int:
    """Exponential backoff with multiplicative 0.5–1.0x jitter.

    ``attempt`` is zero-based (the first retry uses ``attempt=0``).
    Ceiling doubles each attempt; the returned value is a uniform
    random in ``[ceiling/2, ceiling]``. Cap applies after exponentiation.
    """
    ceiling = min(initial_ms * (2 ** attempt), cap_ms)
    lower = ceiling // 2
    return random.randint(lower, ceiling)


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------

_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[-_ ]?after[:=\s]*([0-9]*\.?[0-9]+)", re.IGNORECASE),
)


def parse_retry_after_ms(exc: Exception) -> int | None:
    """Extract a Retry-After hint (in ms) from an exception message.

    Matches ``retry-after: 5`` / ``Retry-After=12s`` / ``retry after 0.5``.
    Returns ``None`` when absent or non-numeric.
    """
    text = str(exc)
    for pat in _RETRY_AFTER_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            seconds = float(m.group(1))
        except ValueError:
            return None
        return int(seconds * 1000)
    return None


# ---------------------------------------------------------------------------
# Retry loop — implemented in Task 4
# ---------------------------------------------------------------------------


async def retry_sdk_call(
    fn: Callable[[], Awaitable[T]],
    *,
    on_retry: Callable[[int, Exception, int], None] | None = None,
) -> T:
    """Run ``fn`` with exponential-backoff retry on transient SDK faults.

    Retries only exceptions classified as TIMEOUT, RATE_LIMIT, or
    SDK_ERROR (via ``agent._classify_error``). Honours a
    ``Retry-After`` hint if the exception message carries one; falls
    back to jittered exponential backoff otherwise.

    ``asyncio.CancelledError`` is re-raised immediately without
    counting as an attempt; a retry loop must not swallow caller
    cancellation (spec 5.2 §3.2 — voice barge-in).
    """
    last_exc: BaseException | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — retry classifies below
            kind = _classify_error(exc)
            if kind not in RETRY_KINDS:
                raise
            last_exc = exc
            is_last = attempt == MAX_ATTEMPTS - 1
            if is_last:
                raise
            hinted = parse_retry_after_ms(exc)
            if hinted is not None:
                delay_ms = hinted
            else:
                delay_ms = compute_backoff_ms(
                    attempt, initial_ms=INITIAL_MS, cap_ms=CAP_MS,
                )
            if on_retry is not None:
                on_retry(attempt, exc, delay_ms)
            await asyncio.sleep(delay_ms / 1000.0)
    # Unreachable: the loop either returns or raises.
    assert last_exc is not None
    raise last_exc
