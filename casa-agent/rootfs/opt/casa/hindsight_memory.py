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
from semantic_memory import SemanticMemory, render_recall

logger = logging.getLogger(__name__)

_DEFAULT_TYPES = ("world", "experience", "observation")


class HindsightSemanticMemory(SemanticMemory):
    def __init__(self, base_url: str, *, timeout_s: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One HTTP round-trip -> parsed JSON. Raises aiohttp errors to caller
        (callers degrade to '' / log per the existing memory-call rule)."""
        url = f"{self._base}{path}"
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.request(method, url, json=payload) as resp:
                resp.raise_for_status()
                return await resp.json()

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

    # ------------------------------------------------------------------
    # Stubs for abstract methods implemented in Tasks 4-5.
    # These bodies are replaced by the real implementations in later tasks.
    # ------------------------------------------------------------------

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
        return render_recall(resp)

    async def profile(self, bank: str) -> str:
        raise NotImplementedError("profile: implemented in Task 4")

    async def cross_recall(
        self, bank: str, query: str, *, max_tokens: int, budget: str = "low",
    ) -> str:
        raise NotImplementedError("cross_recall: implemented in Task 5")
