"""Domain-scope registry — library + embedding-backed router.

`ScopeLibrary` parses + validates the scope policy YAML. `ScopeRegistry`
(Task 4-5) wraps the library with trust-filter helpers and an embedding
model for user-text routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from typing import Any

import yaml
import jsonschema


logger = logging.getLogger(__name__)

# Lazy import — fastembed pulls in onnxruntime, heavy to load at
# interpreter start. The factory indirection also gives tests a
# monkeypatch point.
_DEFAULT_MODEL_NAME = "intfloat/multilingual-e5-large"


def _load_text_embedding_cls():  # pragma: no cover — exercised via monkeypatch
    from fastembed import TextEmbedding  # type: ignore[import-untyped]
    return TextEmbedding


class ScopeError(Exception):
    """Raised on any scope library / registry failure."""


# Schema file ships alongside the disclosure schema in defaults/schema/.
POLICY_SCOPES_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "defaults", "schema", "policy-scopes.v2.json",
)


class ScopeLibrary:
    """Parsed scope definitions — names, minimum_trust, kind, description."""

    def __init__(self, scopes: dict[str, dict[str, Any]]) -> None:
        self._scopes = scopes

    def names(self) -> list[str]:
        return list(self._scopes.keys())

    def get(self, name: str) -> dict[str, Any]:
        if name not in self._scopes:
            raise ScopeError(
                f"unknown scope {name!r}; available: {sorted(self._scopes)}"
            )
        return self._scopes[name]

    def kind(self, name: str) -> str:
        """Return 'topical' or 'system' for *name*. M4 v2 schema."""
        return self.get(name)["kind"]

    def description(self, name: str) -> str:
        # System scopes have no description (schema-enforced).
        return self.get(name).get("description", "")

    def minimum_trust(self, name: str) -> str:
        return self.get(name)["minimum_trust"]


def _load_schema() -> dict[str, Any]:
    with open(POLICY_SCOPES_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_scope_library(path: str) -> ScopeLibrary:
    """Load + validate the scopes YAML at *path*. Raises `ScopeError`."""
    if not os.path.exists(path):
        raise ScopeError(f"scopes file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ScopeError(f"could not parse {path}: {exc}") from exc

    try:
        jsonschema.validate(data, _load_schema())
    except jsonschema.ValidationError as exc:
        raise ScopeError(
            f"{path}: schema violation: {exc.message.casefold()}"
        ) from exc

    return ScopeLibrary(data["scopes"])


# ---------------------------------------------------------------------------
# ScopeRegistry — trust filter + active/argmax helpers
# ---------------------------------------------------------------------------

# Trust ordering, highest → lowest.
_TRUST_ORDER: tuple[str, ...] = (
    "internal",
    "authenticated",
    "external-authenticated",
    "household-shared",
    "public",
)


def _trust_rank(trust: str) -> int:
    """Rank 0 = most privileged. Unknown tiers collapse to `public` (last)."""
    try:
        return _TRUST_ORDER.index(trust)
    except ValueError:
        return len(_TRUST_ORDER) - 1


class ScopeRegistry:
    """Trust filter + embedding-backed scope router.

    Instantiate with a `ScopeLibrary`; the embedding layer is separate
    (`prepare()` — see Task 5). `trust_permits`, `filter_readable`,
    `active_from_scores`, and `argmax_scope` are usable immediately
    without the embedding model.
    """

    def __init__(
        self,
        library: ScopeLibrary,
        *,
        threshold: float = 0.35,
        model_name: str = _DEFAULT_MODEL_NAME,
        embed_cache_size: int = 256,
    ) -> None:
        self._lib = library
        self._threshold = threshold
        self._model_name = model_name
        self._embeddings: dict[str, Any] = {}  # populated by prepare()
        self._degraded: bool = False           # true when model unavailable
        self._model: Any = None
        # Per-process LRU cache for query embeddings. Keyed on
        # text.strip().lower() so voice retriggers and trivial casing
        # variants hit. ~4 KB per entry at 1024-dim float64 × 256 ≈ 1 MB.
        self._embed_cache: OrderedDict[str, Any] = OrderedDict()
        self._embed_cache_max: int = embed_cache_size
        self._embed_hits: int = 0
        self._embed_misses: int = 0

    @property
    def threshold(self) -> float:
        """Score floor used by `active_from_scores` and `argmax_scope`."""
        return self._threshold

    def trust_permits(self, scope: str, channel_trust: str) -> bool:
        """True if a channel at *channel_trust* may access *scope*."""
        scope_min = self._lib.minimum_trust(scope)
        return _trust_rank(channel_trust) <= _trust_rank(scope_min)

    def kind(self, scope: str) -> str:
        """Delegate to library; returns 'topical' | 'system' (M4)."""
        return self._lib.kind(scope)

    def filter_readable(
        self, agent_scopes: list[str], channel_trust: str,
    ) -> list[str]:
        """Intersect agent_scopes with the scopes permitted by *channel_trust*.

        Scopes not known to the library are dropped silently — library is
        the source of truth.
        """
        known = set(self._lib.names())
        return [
            s for s in agent_scopes
            if s in known and self.trust_permits(s, channel_trust)
        ]

    def active_from_scores(
        self,
        scores: dict[str, float],
        default_scope: str,
    ) -> list[str]:
        """Return scopes at or above `threshold`, or a safe fallback.

        Fallback rules:
        - `[default_scope]` if default_scope is in *scores* (i.e., it
          survived the trust filter upstream).
        - `[]` otherwise — the channel's trust tier does not permit
          the agent's default_scope, so no memory is surfaced.
        """
        active = [s for s, v in scores.items() if v >= self._threshold]
        if active:
            return active
        if default_scope in scores:
            return [default_scope]
        return []

    def argmax_scope(
        self,
        scores: dict[str, float],
        default_scope: str,
    ) -> str:
        """Return the highest-scored scope if it clears threshold, else
        *default_scope*."""
        if not scores:
            return default_scope
        winner, top = max(scores.items(), key=lambda kv: kv[1])
        if top < self._threshold:
            return default_scope
        return winner

    # --- embedding layer -------------------------------------------------

    async def prepare(self) -> None:
        """Load the embedding model and embed each scope's description.

        Degrades to a flat-scoring mode on any failure (model download,
        missing file, unexpected exception). Never raises — a broken
        classifier must not block Casa boot.
        """
        try:
            cls = _load_text_embedding_cls()
            self._model = await asyncio.to_thread(
                cls, model_name=self._model_name,
            )
            texts = [self._lib.description(n) for n in self._lib.names()]
            vecs = await asyncio.to_thread(lambda: list(self._model.embed(texts)))
            self._embeddings = dict(zip(self._lib.names(), vecs))
        except Exception as exc:
            logger.error(
                "ScopeRegistry.prepare failed (%s); entering degraded mode",
                exc,
            )
            self._degraded = True

    def _embed_query(self, text: str) -> Any:
        """Embed *text* with LRU caching. Caller must have checked
        that self._model is not None (caller is score()).

        Keyed on normalized text so "Turn off the lights." and "turn
        off the lights" collapse to one cache entry.
        """
        key = text.strip().lower()
        if key in self._embed_cache:
            self._embed_cache.move_to_end(key)
            self._embed_hits += 1
            return self._embed_cache[key]
        vec = next(iter(self._model.embed([text])))
        self._embed_cache[key] = vec
        if len(self._embed_cache) > self._embed_cache_max:
            self._embed_cache.popitem(last=False)
        self._embed_misses += 1
        return vec

    def cache_stats(self) -> tuple[int, int]:
        """Return (hits, misses) for the per-process embedding cache."""
        return self._embed_hits, self._embed_misses

    def score(
        self, text: str, scopes: list[str],
    ) -> dict[str, float]:
        """Return `{scope: cosine_similarity}` for each scope in *scopes*.

        In degraded mode returns 1.0 for every requested scope so that
        the downstream fan-out behaves like pure Revised-A (all scopes
        active). Empty *scopes* input returns empty dict. Query
        embedding is cached per-process (see _embed_query).
        """
        if not scopes:
            return {}
        if self._degraded or self._model is None:
            return {s: 1.0 for s in scopes}

        import numpy as np

        q_vec = self._embed_query(text)
        q_norm = np.linalg.norm(q_vec) or 1.0
        out: dict[str, float] = {}
        for s in scopes:
            v = self._embeddings.get(s)
            if v is None:
                out[s] = 0.0
                continue
            v_norm = np.linalg.norm(v) or 1.0
            out[s] = float(np.dot(q_vec, v) / (q_norm * v_norm))
        return out
