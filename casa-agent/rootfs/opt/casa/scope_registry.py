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
