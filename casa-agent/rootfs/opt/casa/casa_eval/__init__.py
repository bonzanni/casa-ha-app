"""casa_eval — pluggable testers for runtime configuration knobs.

Testers self-register on module import via _register(). Today only
ScopeRoutingTester is wired; future testers will import and register the
same way.
"""

from __future__ import annotations

from casa_eval.base import (
    Case,
    Failure,
    Recommendation,
    Report,
    Suite,
    Tester,
)


_REGISTRY: dict[str, type[Tester]] = {}


def _register(cls: type[Tester]) -> type[Tester]:
    _REGISTRY[cls.id] = cls
    return cls


def list_testers() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_tester(tester_id: str) -> Tester:
    if tester_id not in _REGISTRY:
        raise KeyError(
            f"unknown tester {tester_id!r}; "
            f"available: {list_testers()}"
        )
    return _REGISTRY[tester_id]()


__all__ = [
    "Case", "Failure", "Recommendation", "Report", "Suite", "Tester",
    "list_testers", "get_tester",
]


# Import-side-effect: registers ScopeRoutingTester via @_register.
# Placed at module end so _register is fully defined first.
from casa_eval import scope_routing as _scope_routing  # noqa: F401,E402
