"""Shared test helper for resolving verdict_broker requests.

#469 removed ``VerdictBroker.deliver`` from production — the internal
``/internal/channel/permission_verdict`` endpoint was its only caller, and
verdicts now have exactly one writer (the in-process Telegram callback's
``claim``/``commit``). Tests still need a one-shot resolve; this helper
replicates the old claim→commit composition and return contract
(``"delivered"`` | ``"duplicate"`` | ``"stale"`` | ``"forbidden"``).
"""

from __future__ import annotations


def deliver(
    broker, *, namespace: str, scope: str, request_id: str,
    option_index: int, actor_id: int | None,
) -> str:
    claim = broker.claim(
        namespace=namespace, scope=scope, request_id=request_id,
        option_index=option_index, actor_id=actor_id,
    )
    if isinstance(claim, str):
        return claim
    return "delivered" if broker.commit(claim) else "stale"
