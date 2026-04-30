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

# Streak threshold for the over-budget WARNING (spec §5.2: "three turns
# in a row"). Module-level so tests can read it; not env-tunable per
# spec §9.3 (no env var allocated to item F).
_OVERRUN_STREAK_THRESHOLD = 3
# 10% slack — spec §5.2 ("token_budget * 1.1"). Strict-greater-than so
# exactly 1.1× does not count as overrun.
_OVERRUN_FACTOR = 1.1


class BudgetTracker:
    """Per-session consecutive-overrun streak detector.

    Holds two dicts keyed on ``session_id``:

    * ``_streak`` — current consecutive-overrun count. Resets to 0 when
      a turn comes in under the threshold.
    * ``_warned`` — set of session_ids that have already emitted the
      WARNING. Once warned, suppresses further warnings for that
      session for the rest of the process lifetime; spec §5.2 wants
      "once per (session_id, role) per run".

    Instances are typically held by ``Agent``: one tracker per agent
    role keeps assistant (4000-budget) and butler (800-budget) state
    isolated, even when both serve the same channel concurrently.
    """

    def __init__(self) -> None:
        self._streak: dict[str, int] = {}
        self._warned: set[str] = set()

    def record(self, session_id: str, used_tokens: int, budget: int) -> None:
        # Defensive: misconfigured agent with no/negative budget would
        # otherwise trip every turn forever.
        if budget <= 0:
            return

        threshold = budget * _OVERRUN_FACTOR
        if used_tokens > threshold:
            new_streak = self._streak.get(session_id, 0) + 1
            self._streak[session_id] = new_streak
            if (
                new_streak >= _OVERRUN_STREAK_THRESHOLD
                and session_id not in self._warned
            ):
                self._warned.add(session_id)
                logger.warning(
                    "Memory digest exceeded expected envelope for session %s: "
                    "used=%d budget=%d (>1.1x for %d turns). "
                    "Memory shape may have regressed.",
                    session_id,
                    used_tokens,
                    budget,
                    new_streak,
                )
        else:
            self._streak[session_id] = 0


# ---------------------------------------------------------------------------
# Summary line — implemented in Task 8
# ---------------------------------------------------------------------------


def format_turn_summary(
    role: str,
    channel: str,
    usage: dict[str, int],
) -> str:
    """Render the per-turn ``turn_done`` log line.

    Format::

        turn_done role=<role> channel=<channel> \
            input=<n> output=<n> cache_read=<n> cache_write=<n>

    ``cache_read`` and ``cache_write`` are split (not aggregated):
    cache_read reflects prompt-cache hits (cheap, fast); cache_write
    reflects first-time cache population for the next turn's prefix.
    Tracking them separately surfaces a stable-prefix regression
    (``cache_write > 0`` every turn means our prompt prefix is changing
    each turn and the cache is never paying off).
    """
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return (
        f"turn_done role={role} channel={channel} "
        f"input={inp} output={out} cache_read={cr} cache_write={cw}"
    )
