"""Confirmed-edit settle gating (v0.81.0 W-R1, Sol r2-2).

Three close paths settle a durable open-question ledger entry by editing the
posted keyboard / free-text anchor message to its terminal copy (answered /
expired / superseded) with the keyboard cleared:

  1. the broker finish hook  (``channels.channel_handlers._ask_keyboard_finish``)
  2. boot reconciliation     (``ClaudeCodeDriver.reconcile_open_questions``)
  3. anchor settlement       (``ClaudeCodeDriver._settle_open_anchor``)

``TelegramChannel.edit_topic_message`` returns ``False`` on a transient edit
failure (timeout / BadRequest that is not "not modified") rather than raising.
Historically all three sites IGNORED that return and closed the ledger entry
regardless. Because the broker fires the finish hook exactly ONCE
(``verdict_broker``), a subsequent tap cannot re-drive settlement — so a single
transient edit failure permanently left the keyboard live AND deleted the
recovery record (exactly Nicola's stuck Q1).

This helper bounds-retries the settle edit and reports whether the edit was
CONFIRMED. Callers close the ledger entry ONLY on confirmation; an unconfirmed
edit leaves the entry INTACT so the NEXT boot reconciliation (itself
confirmed-edit gated) settles it. There is deliberately NO later-tap re-drive.

The clock is injectable so tests stay fast and — per the standing memory-cage
rule — NEVER patch ``<module>.asyncio.sleep`` (the shared module attribute).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Exactly three attempts (Sol r2-2). After each FAILED attempt sleep the
# corresponding backoff — 0.5s → 1s → 2s — then give up. Pairing one backoff
# with each attempt keeps the sequence observable end-to-end via the injected
# clock.
SETTLE_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0)


async def confirmed_settle_edit(
    do_edit: Callable[[], Awaitable[bool]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    backoffs: tuple[float, ...] = SETTLE_BACKOFFS,
) -> bool:
    """Attempt ``do_edit`` up to ``len(backoffs)`` times; return ``True`` only on
    a CONFIRMED success.

    ``do_edit`` must return the edit primitive's truthiness —
    ``edit_topic_message`` returns ``True`` on a real edit OR a tolerated
    "message is not modified" (the desired end-state already holds), ``False``
    on a transient failure. A raising edit counts as a failed attempt (this
    helper never propagates). After each failed attempt the corresponding
    backoff is slept, then the loop gives up returning ``False`` — the caller
    must then leave the ledger entry intact.
    """
    ok = False
    for delay in backoffs:
        try:
            ok = bool(await do_edit())
        except Exception:  # noqa: BLE001 — a raising edit is a failed attempt
            logger.debug("settle edit attempt raised", exc_info=True)
            ok = False
        if ok:
            return True
        await sleep(delay)
    return False
