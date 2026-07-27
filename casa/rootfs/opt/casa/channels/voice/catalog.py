"""Authenticated Home Assistant voice-agent catalog."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from channel_authz import agent_allowed_on


VOICE_AGENT_CATALOG_PATH = "/api/voice/agents"
VOICE_AGENT_CATALOG_SCHEMA_VERSION = 1
MAX_VOICE_AGENT_CATALOG_ENTRIES = 20
MAX_VOICE_AGENT_NAME_LENGTH = 128
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class VoiceAgentCatalogError(ValueError):
    """Raised when the complete live catalog cannot be represented safely."""


def _display_name(cfg: Any) -> str:
    character = getattr(cfg, "character", None)
    raw = getattr(character, "name", None)
    if not isinstance(raw, str):
        raise VoiceAgentCatalogError("invalid_name")
    name = raw.strip()
    if not 1 <= len(name) <= MAX_VOICE_AGENT_NAME_LENGTH:
        raise VoiceAgentCatalogError("invalid_name")
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise VoiceAgentCatalogError("invalid_name")
    return name


def build_voice_agent_catalog(
    agent_configs: Mapping[str, Any],
) -> dict[str, object]:
    """Build a complete deterministic catalog or fail without partial output."""
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for mapping_role, cfg in agent_configs.items():
        if getattr(cfg, "enabled", True) is not True:
            continue
        if not agent_allowed_on("voice", cfg):
            continue
        role = getattr(cfg, "role", None)
        if (
            role != mapping_role
            or not isinstance(role, str)
            or not _ROLE_RE.fullmatch(role)
        ):
            raise VoiceAgentCatalogError("invalid_role")
        if role in seen:
            raise VoiceAgentCatalogError("duplicate_role")
        seen.add(role)
        candidates.append({"role": role, "name": _display_name(cfg)})
    if len(candidates) > MAX_VOICE_AGENT_CATALOG_ENTRIES:
        raise VoiceAgentCatalogError("too_many_agents")
    candidates.sort(key=lambda item: item["role"])
    return {
        "schema_version": VOICE_AGENT_CATALOG_SCHEMA_VERSION,
        "agents": candidates,
    }
