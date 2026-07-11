"""Warm SDK-client pool for resident turns (spec 2026-07-11, AR-1..AR-10).

One ``ManagedSdkClient`` == one live conversation (subprocess + MCP
handshake kept warm across turns). ``SdkClientPool`` caches at most one
per ``channel_key`` per Agent, reconciled against the SessionRegistry —
the registry stays the sole source of truth for which conversation a key
is on; the pool only caches a live client *for* that conversation.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class _CidBox:
    """Mutable cid holder. Bound into ``log_cid.cid_var`` in the client's
    connect context so log records created inside the SDK read task carry
    the *current turn's* cid (the read task snapshots contextvars at
    connect — F7); ``run_turn_locked`` rewrites ``value`` per turn."""

    def __init__(self) -> None:
        self.value = "-"

    def __str__(self) -> str:
        return self.value


class SdkTurnError(Exception):
    """CLI returned an is_error ResultMessage whose text classifies as a
    retryable fault (AR-5). Message text == the result text so
    error_kinds._classify_error routes it to RETRY_KINDS."""


class PoolUnavailable(Exception):
    """Pool is closing or the entry vanished twice — caller must run the
    turn on the per-turn bypass path instead (AR-7)."""


def _default_make_client(options):
    from claude_agent_sdk import ClaudeSDKClient
    return ClaudeSDKClient(options)


class ManagedSdkClient:
    """One warm conversation: a live ClaudeSDKClient + the contextvar
    bindings its read task snapshotted at connect (F7).

    Locking contract: callers hold ``self.lock`` around
    ``run_turn_locked`` (the pool does; the bypass path does too).
    ``open()``/``aclose()`` manage their own consistency.
    """

    def __init__(self, options, *, origin_ctxvar, cid_ctxvar,
                 engagement_ctxvar, make_client=None,
                 monotonic=time.monotonic) -> None:
        self.options = options
        self._origin_ctxvar = origin_ctxvar
        self._cid_ctxvar = cid_ctxvar
        self._engagement_ctxvar = engagement_ctxvar
        self._make_client = make_client or _default_make_client
        self._monotonic = monotonic
        self.origin_holder: dict = {}
        self.cid_box = _CidBox()
        self.lock = asyncio.Lock()
        self.state = "new"
        self.sid: str | None = None
        self.created_at = monotonic()
        self.last_used = monotonic()
        self._client: Any = None

    async def open(self) -> None:
        """Connect under a context that binds origin/cid holders.

        The SDK's read task is created inside connect() and snapshots the
        ambient context (F7) — so the holders MUST be bound before the
        connect call, and engagement_var MUST be None (resident turns
        never run inside an engagement binding — spec Q7)."""
        assert self._engagement_ctxvar.get(None) is None, (
            "ManagedSdkClient.open() inside an engagement binding"
        )
        assert self.state == "new", f"open() on state={self.state}"
        self._client = self._make_client(self.options)

        ctx = contextvars.copy_context()

        def _bind() -> None:
            self._origin_ctxvar.set(self.origin_holder)
            self._cid_ctxvar.set(self.cid_box)

        ctx.run(_bind)
        # Run connect inside the prepared context so the read task
        # inherits the holder bindings.
        task = asyncio.get_running_loop().create_task(
            self._connect(), context=ctx,
        )
        try:
            await task
        except BaseException:
            self.state = "invalid"
            raise
        self.state = "warm"

    async def _connect(self) -> None:
        connect = getattr(self._client, "connect", None)
        if connect is not None:
            await connect()
        else:  # pragma: no cover — ClaudeSDKClient always has connect()
            await self._client.__aenter__()

    async def run_turn_locked(
        self, prompt: str, *, origin: dict, cid: str,
        on_message: Callable[[Any], Awaitable[None]],
    ) -> str | None:
        """Run one turn on the warm client. Caller holds ``self.lock``.

        Rewrites the origin holder + cid box IN PLACE (read-task
        visibility — spec Q7), queries, iterates receive_response()
        forwarding every message to ``on_message``, captures the sid,
        and enforces the AR-5 error-result and AR-1 cancellation
        contracts."""
        from claude_agent_sdk import ResultMessage, SystemMessage

        assert self.state == "warm", f"run_turn on state={self.state}"
        self.state = "in_turn"
        self.origin_holder.clear()
        self.origin_holder.update(origin)
        self.cid_box.value = cid or "-"
        self.last_used = self._monotonic()
        result_msg = None
        try:
            await self._client.query(prompt)
            async for sdk_msg in self._client.receive_response():
                if isinstance(sdk_msg, SystemMessage):
                    if getattr(sdk_msg, "subtype", None) == "init":
                        data = getattr(sdk_msg, "data", {}) or {}
                        if "session_id" in data:
                            self.sid = data["session_id"]
                elif isinstance(sdk_msg, ResultMessage):
                    result_msg = sdk_msg
                    s = getattr(sdk_msg, "session_id", None)
                    if s:
                        self.sid = s
                await on_message(sdk_msg)
        except asyncio.CancelledError:
            await self._cleanup_after_cancel()
            raise
        except BaseException:
            await self._invalidate()
            raise
        self.last_used = self._monotonic()
        # AR-5: never leave an error-result entry warm; raise retryables.
        if result_msg is not None and getattr(result_msg, "is_error", False):
            await self._invalidate()
            text = str(getattr(result_msg, "result", "") or "")
            from error_kinds import _classify_error
            from retry import RETRY_KINDS
            if _classify_error(SdkTurnError(text)) in RETRY_KINDS:
                raise SdkTurnError(text)
            self.state = "invalid"  # non-retryable: surfaced text, dead entry
            return self.sid
        self.state = "warm"
        return self.sid

    async def _cleanup_after_cancel(self) -> None:
        """AR-1/AR-10: interrupt (≤2 s) then drain the aborted turn's
        buffered messages through its ResultMessage (≤5 s), shielded from
        further cancellation; ANY failure (incl. a second CancelledError)
        invalidates instead of returning the entry to warm."""
        try:
            await asyncio.shield(
                asyncio.wait_for(self._interrupt_and_drain(), timeout=7.0)
            )
            self.state = "warm"
        except BaseException:  # noqa: BLE001 — second cancel included (AR-10)
            await self._invalidate()

    async def _interrupt_and_drain(self) -> None:
        from claude_agent_sdk import ResultMessage
        await asyncio.wait_for(self._client.interrupt(), timeout=2.0)
        async for sdk_msg in self._client.receive_response():
            if isinstance(sdk_msg, ResultMessage):
                s = getattr(sdk_msg, "session_id", None)
                if s:
                    self.sid = s
                return

    async def _invalidate(self) -> None:
        self.state = "invalid"
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("pool client disconnect failed: %s", exc)

    async def aclose(self) -> None:
        if self.state == "closed":
            return
        client, self._client = self._client, None
        self.state = "closed"
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("pool client close failed: %s", exc)
