# casa-agent/rootfs/opt/casa/semantic_memory.py
"""Long-term semantic-memory seam (memory re-architecture spec §5).

A small interface shaped to a best-in-class backend (Hindsight), with a
NoOp degraded impl (empty strings → the agent runs cold on its SDK thread).
Reads return rendered markdown digests for the system prompt; ``retain``
is fire-and-forget (None). Short-term/recency is NOT here — that is owned
by the SDK session.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


def render_mental_models(response: dict[str, Any]) -> str:
    """Render a mental-model list response into a digest. Tolerant of the
    list key name (``mental_models``/``models``/``items``)."""
    resp = response or {}
    models = resp.get("mental_models") or resp.get("models") or resp.get("items") or []
    lines: list[str] = []
    for m in models:
        content = (m.get("content") or "").strip() if isinstance(m, dict) else ""
        if content:
            lines.append(content)
    return "\n\n".join(lines)


def render_recall(response: dict[str, Any]) -> str:
    """Render a Hindsight recall response into a markdown digest.

    Shape (spec §8 findings): ``{"results": [{"text": str, "type": str,
    "tags": [str], ...}, ...]}``. One bullet per fact; empty/missing →
    empty string (no placeholder lines)."""
    results = (response or {}).get("results") or []
    lines: list[str] = []
    for r in results:
        text = (r.get("text") or "").strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


class SemanticMemory(ABC):
    """Long-term memory backend. Banks are addressed by id (see hindsight_ids)."""

    @abstractmethod
    async def retain(
        self, bank: str, items: list[dict[str, Any]], *, async_: bool = True,
    ) -> None:
        """Persist memory items into ``bank`` (LLM fact-extraction, async by
        default). Each item: ``{content(req), context, timestamp, tags,
        metadata, document_id}``."""

    @abstractmethod
    async def recall(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        types: tuple[str, ...] = ("world", "experience", "observation"),
        tags_match: str = "any", budget: str = "mid",
    ) -> str:
        """Return a rendered digest of facts relevant to ``query`` in ``bank``.
        ``types`` MUST keep ``world`` or raw facts are dropped (spec §8.9)."""

    @abstractmethod
    async def profile(self, bank: str) -> str:
        """Return the bank's mental-model overlay digest (cheap GET, no LLM)."""


class NoOpSemanticMemory(SemanticMemory):
    """Degraded backend: retain is silent, reads return ''. The agent then
    runs on its SDK thread alone (cold long-term)."""

    async def retain(
        self, bank: str, items: list[dict[str, Any]], *, async_: bool = True,
    ) -> None:
        return None

    async def recall(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        types: tuple[str, ...] = ("world", "experience", "observation"),
        tags_match: str = "any", budget: str = "mid",
    ) -> str:
        return ""

    async def profile(self, bank: str) -> str:
        return ""
