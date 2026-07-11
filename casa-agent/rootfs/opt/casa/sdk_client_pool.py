"""Warm SDK-client pool for resident turns (spec 2026-07-11, AR-1..AR-10).

One ``ManagedSdkClient`` == one live conversation (subprocess + MCP
handshake kept warm across turns). ``SdkClientPool`` caches at most one
per ``channel_key`` per Agent, reconciled against the SessionRegistry —
the registry stays the sole source of truth for which conversation a key
is on; the pool only caches a live client *for* that conversation.
"""

from __future__ import annotations

import time


class _CidBox:
    """Mutable cid holder. Bound into ``log_cid.cid_var`` in the client's
    connect context so log records created inside the SDK read task carry
    the *current turn's* cid (the read task snapshots contextvars at
    connect — F7); ``run_turn_locked`` rewrites ``value`` per turn."""

    def __init__(self) -> None:
        self.value = "-"

    def __str__(self) -> str:
        return self.value
