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
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, NamedTuple

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


def _env_int(name: str, default: int, *, min_value: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    return max(value, min_value)


def pool_enabled() -> bool:
    return os.environ.get("SDK_CLIENT_POOL", "on").strip().lower() not in (
        "off", "0", "false",
    )


class PoolTurnResult(NamedTuple):
    sid: str | None
    resume_sid: str | None
    is_fresh: bool


class SdkClientPool:
    """Per-Agent cache of warm conversation clients, keyed by channel_key.

    The SessionRegistry stays authoritative: the resume decision is derived
    INSIDE turn(), under the entry's serialization (AR-3), from a fresh
    registry read — so /new, freshness expiry, sid drift, and interleaved
    turns can never fork a conversation or reuse a stale client."""

    _instances: "list[SdkClientPool]" = []   # fleet accounting (Task 6)

    def __init__(self, session_registry, *, decide, origin_ctxvar, cid_ctxvar,
                 engagement_ctxvar, freshness=None, make_client=None,
                 monotonic=time.monotonic, wall_now=None) -> None:
        if freshness is None:
            from session_saver import freshness_window as freshness
        self._registry = session_registry
        self._decide = decide
        self._origin_ctxvar = origin_ctxvar
        self._cid_ctxvar = cid_ctxvar
        self._engagement_ctxvar = engagement_ctxvar
        self._freshness = freshness
        self._make_client = make_client
        self._monotonic = monotonic
        self._wall_now = wall_now or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, ManagedSdkClient] = {}
        self._pool_lock = asyncio.Lock()
        self._closing = False
        self._sweeper: asyncio.Task | None = None
        self.max_per_agent = _env_int("SDK_POOL_MAX_PER_AGENT", 4)
        self.idle_seconds = float(_env_int("SDK_POOL_IDLE_SECONDS", 1800))
        self.max_age_seconds = float(_env_int("SDK_POOL_MAX_AGE_SECONDS", 43200))
        SdkClientPool._instances.append(self)

    def stats(self) -> dict:
        return {"entries": len(self._entries), "closing": self._closing}

    async def turn(self, *, channel_key: str, channel: str, prompt: str,
                   origin: dict, cid: str, build_options, on_stale_old,
                   on_message) -> PoolTurnResult:
        self._ensure_sweeper()
        for _attempt in (1, 2):                      # AR-7: one silent retry
            if self._closing:
                raise PoolUnavailable("pool closing")
            entry = await self._entry_stub(channel_key)
            result: PoolTurnResult | None = None
            async with entry.lock:
                if self._entries.get(channel_key) is not entry:
                    continue                          # replaced/evicted; retry
                if entry.state not in ("new", "warm"):
                    async with self._pool_lock:
                        if self._entries.get(channel_key) is entry:
                            del self._entries[channel_key]
                    continue
                # --- decision UNDER the entry lock (AR-3) ---
                reg_entry = self._registry.get(channel_key)
                decision, save_old = self._decide(
                    channel, reg_entry, self._wall_now(),
                )
                resume_sid = (
                    reg_entry.get("sdk_session_id")
                    if decision == "resume" and reg_entry else None
                )
                is_fresh = resume_sid is None
                reusable = (
                    entry.state == "warm"
                    and not is_fresh
                    and entry.sid == resume_sid
                )
                if not reusable:
                    old_sid = entry.sid
                    if entry.state == "warm":
                        await entry.aclose()          # flush BEFORE retain (AR-4)
                    if save_old:
                        stale = (reg_entry or {}).get("sdk_session_id") or old_sid
                        if stale:
                            on_stale_old(stale)
                    options = await build_options(is_fresh, resume_sid)
                    fresh_client = ManagedSdkClient(
                        options,
                        origin_ctxvar=self._origin_ctxvar,
                        cid_ctxvar=self._cid_ctxvar,
                        engagement_ctxvar=self._engagement_ctxvar,
                        make_client=self._make_client or _default_make_client,
                        monotonic=self._monotonic,
                    )
                    fresh_client.lock = entry.lock    # keep the held lock
                    fresh_client.sid = resume_sid
                    await fresh_client.open()
                    async with self._pool_lock:
                        self._entries[channel_key] = fresh_client
                    entry = fresh_client
                if decision == "resume":
                    await self._registry.touch(channel_key)
                try:
                    sid = await entry.run_turn_locked(
                        prompt, origin=origin, cid=cid, on_message=on_message,
                    )
                except asyncio.CancelledError:
                    if entry.state != "warm":
                        await self._drop(channel_key, entry)
                    raise
                except BaseException:
                    await self._drop(channel_key, entry)
                    raise
                if entry.state != "warm":             # AR-5 non-retryable path
                    await self._drop(channel_key, entry)
                result = PoolTurnResult(sid=sid, resume_sid=resume_sid,
                                        is_fresh=is_fresh)
            if result is not None:
                await self._enforce_caps(channel_key)  # outside entry.lock
                return result
        raise PoolUnavailable("entry unstable after retry")

    async def _entry_stub(self, channel_key: str) -> ManagedSdkClient:
        async with self._pool_lock:
            entry = self._entries.get(channel_key)
            if entry is None:
                entry = ManagedSdkClient(
                    None,
                    origin_ctxvar=self._origin_ctxvar,
                    cid_ctxvar=self._cid_ctxvar,
                    engagement_ctxvar=self._engagement_ctxvar,
                    make_client=self._make_client or _default_make_client,
                    monotonic=self._monotonic,
                )
                self._entries[channel_key] = entry
            return entry

    async def _drop(self, channel_key: str, entry: ManagedSdkClient) -> None:
        """Generation-checked invalidate: only removes THIS entry object."""
        async with self._pool_lock:
            if self._entries.get(channel_key) is entry:
                del self._entries[channel_key]
        await entry.aclose()

    async def close_key(self, channel_key: str) -> None:
        """AR-4 reset-hook target: close (flush) the key's warm client."""
        async with self._pool_lock:
            entry = self._entries.pop(channel_key, None)
        if entry is not None:
            async with entry.lock:
                await entry.aclose()

    async def aclose(self, *, drain_timeout: float = 120.0) -> None:
        self._closing = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        async with self._pool_lock:
            entries = dict(self._entries)
            self._entries.clear()
        for key, entry in entries.items():
            try:
                await asyncio.wait_for(entry.lock.acquire(), timeout=drain_timeout)
                try:
                    await entry.aclose()
                finally:
                    entry.lock.release()
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning("pool aclose: drain timeout on %s; force close", key)
                await entry.aclose()
        if self in SdkClientPool._instances:
            SdkClientPool._instances.remove(self)

    def _channel_of(self, channel_key: str) -> str:
        return channel_key.partition("-")[0]

    async def _enforce_caps(self, protect: str) -> None:
        """Caller holds no locks. LRU-close overage; never the protected key."""
        fleet_cap = _env_int("SDK_POOL_FLEET_CAP", 8)

        def _lru(pools):
            candidates = [
                (e.last_used, p, k, e)
                for p in pools
                for k, e in p._entries.items()
                if e.state == "warm" and not (p is self and k == protect)
            ]
            return min(candidates, default=None)

        while len(self._entries) > self.max_per_agent:
            victim = _lru([self])
            if victim is None:
                break
            _, p, k, e = victim
            logger.info("pool cap: LRU-closing %s", k)
            await p._drop(k, e)
        while sum(len(p._entries) for p in SdkClientPool._instances) > fleet_cap:
            victim = _lru(SdkClientPool._instances)
            if victim is None:
                break
            _, p, k, e = victim
            logger.info("fleet cap: LRU-closing %s", k)
            await p._drop(k, e)

    async def _sweep_once(self) -> None:
        now = self._monotonic()
        doomed: list[tuple[str, ManagedSdkClient]] = []
        async with self._pool_lock:
            for key, e in list(self._entries.items()):
                if e.state != "warm":
                    continue
                bound = min(
                    self._freshness(self._channel_of(key)).total_seconds(),
                    self.idle_seconds,
                )
                if (now - e.last_used) > bound or \
                        (now - e.created_at) > self.max_age_seconds:
                    doomed.append((key, e))
        for key, e in doomed:
            logger.info("pool sweep: closing %s (idle/max-age)", key)
            await self._drop(key, e)

    async def _run_sweeper(self, interval: float = 60.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("pool sweep failed; retrying next tick")
        except asyncio.CancelledError:
            return

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(
                self._run_sweeper(), name="sdk-pool-sweeper",
            )
