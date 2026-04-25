"""Bidirectional name↔role map across residents and specialists.

Built once at boot from the loaded AgentConfig dicts and rebuilt on
casa_reload. Read-only at runtime. Used by:

- `agent.py` to render `<delegates>` / `<executors>` system-prompt blocks
  with display names (e.g. "Tina (role: butler)").
- Future code paths that need programmatic name resolution.

Internal addressing in tools and registries stays role-keyed; this
module is a rendering / utility layer only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import AgentConfig


Tier = Literal["resident", "specialist"]


@dataclass(frozen=True)
class KnownAgent:
    role: str
    name: str
    card: str
    tier: Tier


class AgentRegistry:
    """Read-only lookup. Build via :meth:`AgentRegistry.build`."""

    def __init__(
        self,
        *,
        role_to_known: dict[str, KnownAgent],
        name_lower_to_role: dict[str, str],
    ) -> None:
        self._role_to_known = role_to_known
        self._name_lower_to_role = name_lower_to_role

    # --- factory --------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        residents: dict[str, AgentConfig],
        specialists: dict[str, AgentConfig],
    ) -> "AgentRegistry":
        role_to_known: dict[str, KnownAgent] = {}
        name_lower_to_role: dict[str, str] = {}

        def _register(role: str, cfg: AgentConfig, tier: Tier) -> None:
            name = (getattr(cfg.character, "name", "") or role).strip()
            card = getattr(cfg.character, "card", "") or ""
            role_to_known[role] = KnownAgent(
                role=role, name=name, card=card, tier=tier,
            )
            name_lower_to_role.setdefault(name.lower(), role)

        for role, cfg in residents.items():
            _register(role, cfg, "resident")
        for role, cfg in specialists.items():
            _register(role, cfg, "specialist")

        return cls(
            role_to_known=role_to_known,
            name_lower_to_role=name_lower_to_role,
        )

    # --- queries --------------------------------------------------------

    def role_to_name(self, role: str) -> str:
        known = self._role_to_known.get(role)
        return known.name if known else role

    def name_to_role(self, name: str) -> str | None:
        return self._name_lower_to_role.get((name or "").strip().lower())

    def all_known(self) -> list[KnownAgent]:
        return list(self._role_to_known.values())
