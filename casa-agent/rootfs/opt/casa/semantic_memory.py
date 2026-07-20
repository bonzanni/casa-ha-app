# casa-agent/rootfs/opt/casa/semantic_memory.py
"""Long-term semantic-memory seam (memory re-architecture spec §5).

A small interface shaped to a best-in-class backend (Hindsight), with a
NoOp degraded impl (recall unavailable, silent writes → the agent runs cold
on its SDK thread).
Reads return rendered markdown digests for the system prompt; ``retain``
is fire-and-forget (None). Short-term/recency is NOT here — that is owned
by the SDK session.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RecallUnavailable(RuntimeError):
    """Semantic recall could NOT be performed — timeout, 5xx/429, transport
    failure, or a malformed response envelope.

    Three-outcome contract (v0.99.0): a recall is either (1) hits, (2) a
    genuine zero-hit '' from a well-formed 2xx response, or (3) this
    exception. Backends and bridges must never collapse a failure into '',
    which callers cannot tell from "searched and found nothing" — that is
    how agents end up truthfully-looking denying knowledge they have.

    ``reason`` is a stable slug (``timeout``, ``http_504``, ``transport``,
    ``malformed_envelope``, ``backend_error``) safe to log; it never carries
    query text or recalled content.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"recall unavailable: {reason}")


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

    async def close(self) -> None:
        """Release any backend resources (e.g. a pooled HTTP session).

        Concrete (non-abstract) no-op default so backends that hold nothing
        (NoOpSemanticMemory) need not override it; HTTP-backed backends
        override to close their shared client session on shutdown."""
        return None


class NoOpSemanticMemory(SemanticMemory):
    """Degraded backend: retain is silent, profile returns ''. The agent then
    runs on its SDK thread alone (cold long-term).

    ``recall`` raises :class:`RecallUnavailable` (v0.99.0): a NoOp cannot
    CHECK memory, and returning '' would fabricate a genuine-looking zero-hit
    search — agents would then claim "nothing found" where no search ever
    ran. The overlay (``profile``) stays a silent '' because nothing claims
    absence from a missing overlay."""

    async def retain(
        self, bank: str, items: list[dict[str, Any]], *, async_: bool = True,
    ) -> None:
        return None

    async def recall(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        types: tuple[str, ...] = ("world", "experience", "observation"),
        tags_match: str = "any", budget: str = "mid",
    ) -> str:
        raise RecallUnavailable("not_configured")

    async def profile(self, bank: str) -> str:
        return ""
