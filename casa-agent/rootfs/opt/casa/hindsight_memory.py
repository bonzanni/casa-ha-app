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
        # object synchronously). Reused across calls so the per-message memory
        # round-trips share one keep-alive connection pool (L32) instead of
        # opening + tearing down a fresh TCP connection every call.
        self._session: aiohttp.ClientSession | None = None

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One HTTP round-trip -> parsed JSON. Raises aiohttp errors to caller
        (callers degrade to '' / log per the existing memory-call rule)."""
        url = f"{self._base}{path}"
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        async with self._session.request(method, url, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

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

