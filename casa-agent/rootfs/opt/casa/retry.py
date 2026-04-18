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
    """Run ``fn`` with retry + backoff on transient SDK classes.

    Implemented in Task 4 — this stub lets Task 2 tests compile.
    """
    raise NotImplementedError("implemented in Task 4")
