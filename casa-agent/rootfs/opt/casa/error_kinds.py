"""Error classification helpers shared by agent and retry modules.

Extracted to a standalone module to avoid the circular-import that arises
when ``agent`` imports ``retry`` *and* ``retry`` imports from ``agent``.
"""

from __future__ import annotations

import asyncio
from enum import Enum


class ErrorKind(Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SDK_ERROR = "sdk_error"
    MEMORY_ERROR = "memory_error"
    CHANNEL_ERROR = "channel_error"
    UNKNOWN = "unknown"


_USER_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.TIMEOUT: "The request timed out. Try again in a moment.",
    ErrorKind.RATE_LIMIT: "Rate limited by the API. Please wait a minute and try again.",
    ErrorKind.SDK_ERROR: "There was an issue communicating with Claude. Please try again.",
    ErrorKind.MEMORY_ERROR: "Memory service is unavailable, but I can still respond without context.",
    ErrorKind.CHANNEL_ERROR: "There was an issue sending the response.",
    ErrorKind.UNKNOWN: "Sorry, something went wrong while processing your request.",
}


def _classify_error(exc: Exception) -> ErrorKind:
    """Classify an exception into an ErrorKind for routing recovery."""
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorKind.TIMEOUT

    msg = str(exc).lower()
    if "rate" in msg and "limit" in msg:
        return ErrorKind.RATE_LIMIT
    if "429" in msg:
        return ErrorKind.RATE_LIMIT
    if "timeout" in msg or "timed out" in msg:
        return ErrorKind.TIMEOUT

    exc_type = type(exc).__name__
    if "CLI" in exc_type or "SDK" in exc_type or "Connection" in exc_type:
        return ErrorKind.SDK_ERROR

    return ErrorKind.UNKNOWN
