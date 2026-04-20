"""Domain-scope registry — library + embedding-backed router.

`ScopeLibrary` parses + validates the scope policy YAML. `ScopeRegistry`
(Task 4-5) wraps the library with trust-filter helpers and an embedding
model for user-text routing.
"""

from __future__ import annotations

import json
import os
from typing import Any

import yaml
import jsonschema


class ScopeError(Exception):
    """Raised on any scope library / registry failure."""


# Schema file ships alongside the disclosure schema in defaults/schema/.
POLICY_SCOPES_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "defaults", "schema", "policy-scopes.v1.json",
)


class ScopeLibrary:
    """Parsed scope definitions — names, minimum_trust, description."""

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

    def description(self, name: str) -> str:
        return self.get(name)["description"]

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
    ) -> None:
        self._lib = library
        self._threshold = threshold
        self._embeddings: dict[str, Any] = {}  # populated by prepare()
        self._degraded: bool = False           # true when model unavailable
        self._model: Any = None

    def trust_permits(self, scope: str, channel_trust: str) -> bool:
        """True if a channel at *channel_trust* may access *scope*."""
        scope_min = self._lib.minimum_trust(scope)
        return _trust_rank(channel_trust) <= _trust_rank(scope_min)

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
        """Return scopes at or above `threshold`, or `[default_scope]` when
        nothing clears the bar."""
        active = [s for s, v in scores.items() if v >= self._threshold]
        if active:
            return active
        return [default_scope]

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
