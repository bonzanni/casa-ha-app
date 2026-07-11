# casa-agent/rootfs/opt/casa/hindsight_memory.py
"""Hindsight HTTP implementation of SemanticMemory (spec §4, verified §8).

Talks to the bank API at ``{base_url}/v1/default/banks/{bank}/...``. The
base URL is configurable (the add-on is reachable via its hassio network
alias / IP, NOT the literal host ``hindsight`` -- spec §8.8). API is
unauthenticated on the internal network (spec §8.4).
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from hindsight_ids import bank_id as _validate_bank_id  # fail-fast on bad ids
from semantic_memory import SemanticMemory, render_mental_models, render_recall

logger = logging.getLogger(__name__)

_DEFAULT_TYPES = ("world", "experience", "observation")


class HindsightSemanticMemory(SemanticMemory):
    def __init__(self, base_url: str, *, timeout_s: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        # Lazily created inside a running event loop (tests construct this
        # object synchronously). One ClientSession is reused across calls, but
        # its connector uses ``force_close`` (see _new_session) so no TCP
        # connection is pooled between calls.
        self._session: aiohttp.ClientSession | None = None

    def _new_session(self) -> aiohttp.ClientSession:
        # D-3 (2026-07-11): the client previously pooled keep-alive
        # connections. Memory round-trips are sparse and bursty (1-2 per turn,
        # turns minutes+ apart), so a pooled connection was almost always idle
        # past Hindsight's keep-alive window; the FIRST round-trip of a turn
        # (the recall) then reused a half-closed socket and raised
        # ServerDisconnectedError on ``await protocol.read()``, silently
        # degrading memory for hours while the same-turn retain (fresh
        # connection) still succeeded. ``force_close`` opens one fresh
        # connection per call — correct for this traffic shape, and the
        # keep-alive it dropped was never actually reused between turns anyway.
        return aiohttp.ClientSession(
            timeout=self._timeout,
            connector=aiohttp.TCPConnector(force_close=True),
        )

    async def _roundtrip(
        self, method: str, url: str, payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assert self._session is not None  # set by _request before calling
        async with self._session.request(method, url, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One HTTP round-trip -> parsed JSON. Raises aiohttp errors to caller
        (callers degrade to '' / log per the existing memory-call rule)."""
        url = f"{self._base}{path}"
        if self._session is None or self._session.closed:
            self._session = self._new_session()
        try:
            return await self._roundtrip(method, url, payload)
        except aiohttp.ClientConnectionError:
            # Belt to force_close's root-cause fix: a genuine mid-call drop
            # (ServerDisconnectedError / ClientOSError, both subclasses) means
            # no response was received, so aiohttp has discarded the dead
            # transport and a single retry gets a fresh connection. Scoped to
            # connection errors ONLY: an HTTP 4xx/5xx (ClientResponseError, not
            # a ClientConnectionError) means the request WAS received, so a
            # retained write may have landed — retrying it could double-write.
            return await self._roundtrip(method, url, payload)

    async def close(self) -> None:
        """Close the shared client session (called on shutdown so aiohttp
        does not emit an 'Unclosed client session' warning)."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def retain(
        self, bank: str, items: list[dict[str, Any]], *, async_: bool = True,
    ) -> None:
        # ``bank`` is already a built id (e.g. "casa-assistant"); a single-part
        # bank_id() call re-validates charset + length and raises ValueError on
        # a malformed id before any HTTP (Hindsight silently accepts bad ids).
        _validate_bank_id(bank)
        await self._request(
            "POST", f"/v1/default/banks/{bank}/memories",
            {"async": async_, "items": items},
        )
        # E1 (observability): retains were previously silent on success, so a
        # working memory write left no trace in the logs — only failures logged.
        logger.info(
            "memory_retain bank=%s items=%d async=%s", bank, len(items), async_,
        )

    async def recall(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        types: tuple[str, ...] = _DEFAULT_TYPES,
        tags_match: str = "any", budget: str = "mid",
    ) -> str:
        _validate_bank_id(bank)
        resp = await self._request(
            "POST", f"/v1/default/banks/{bank}/memories/recall",
            {
                "query": query, "tags": tags, "tags_match": tags_match,
                "max_tokens": max_tokens, "types": list(types), "budget": budget,
            },
        )
        # E1 (observability): recalls were silent, so an empty recall was
        # indistinguishable from a recall never happening. Log the hit count
        # and the clearance tags used (never the query text — may be sensitive).
        hits = resp.get("results") or resp.get("memories") or []
        logger.info(
            "memory_recall bank=%s tags=%s hits=%d",
            bank, tags, len(hits) if isinstance(hits, list) else 0,
        )
        return render_recall(resp)

    async def profile(self, bank: str) -> str:
        _validate_bank_id(bank)
        resp = await self._request(
            "GET", f"/v1/default/banks/{bank}/mental-models", None,
        )
        return render_mental_models(resp)

