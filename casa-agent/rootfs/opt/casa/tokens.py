"""Token accounting + budget monitoring (spec 5.2 §5, descoped — no cost).

Casa runs on a Claude Max subscription, so per-token USD pricing is
fictional. This module ships only the parts that pay rent independent
of billing model:

* :func:`estimate_tokens` + :class:`BudgetTracker` — catch a memory
  backend that fails to honour ``memory.token_budget`` (silent overrun
  is the bug class).
* :func:`extract_usage` + :func:`format_turn_summary` — one-line
  per-turn telemetry of ``input / output / cache_read / cache_write``.
  Useful for prompt-cache validation (cache hit visibility) and
  200k-context-window proximity. No derived metrics, no warnings —
  raw counts only; operators do their own analysis from logs.

All counters are in-process; restart resets them. No env vars, no
metrics endpoint, no dashboard surface (spec §5.3, §9.3).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------


def estimate_tokens(text: str | None) -> int:
    """Cheap char-to-token estimate: ``len(text) // 4``.

    Treats ``None`` and ``""`` as zero so callers do not need to guard
    against empty memory digests on the first turn of a fresh session.
    """
    if not text:
        return 0
    return len(text) // 4


# ---------------------------------------------------------------------------
# Usage extraction — implemented in Task 4
# ---------------------------------------------------------------------------


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def extract_usage(result_msg: object) -> dict[str, int]:
    """Pull token counts off an SDK ``ResultMessage``.

    Reads ``result_msg.usage`` (attribute), expects either a dict or
    ``None``. Missing fields default to 0. String values are coerced to
    int; non-numeric values fall back to 0 so a malformed SDK payload
    cannot crash ``Agent._process``.
    """
    usage = getattr(result_msg, "usage", None) or {}
    out: dict[str, int] = {}
    for key in _USAGE_KEYS:
        raw = usage.get(key, 0) if isinstance(usage, dict) else getattr(
            usage, key, 0,
        )
        try:
            out[key] = int(raw or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


# ---------------------------------------------------------------------------
# Budget tracker — implemented in Task 6
# ---------------------------------------------------------------------------


class BudgetTracker:
    """Per-session consecutive-overrun streak detector.

    Implemented in Task 6; stub instantiates so imports succeed.
    """

    def __init__(self) -> None:
        raise NotImplementedError("implemented in Task 6")

    def record(self, session_id: str, used_tokens: int, budget: int) -> None:
        raise NotImplementedError("implemented in Task 6")


# ---------------------------------------------------------------------------
# Summary line — implemented in Task 8
# ---------------------------------------------------------------------------


def format_turn_summary(
    role: str,
    channel: str,
    usage: dict[str, int],
) -> str:
    """Render the per-turn ``turn_done`` log line.

    Implemented in Task 8; stub raises.
    """
    raise NotImplementedError("implemented in Task 8")
