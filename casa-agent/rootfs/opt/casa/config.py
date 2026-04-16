"""Configuration loading and model mapping for Casa agents."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# Deprecated role name -> canonical role. Logged at WARNING on load.
ROLE_ALIASES: dict[str, str] = {
    "main": "assistant",
}


def _normalize_role(raw: str, source: str) -> str:
    """Validate and normalize a role value.

    Empty string raises ValueError. Deprecated aliases (e.g. ``main``)
    are mapped to their canonical form with a warning.
    """
    if not raw:
        raise ValueError(
            f"Missing required 'role' field in agent config {source!r}. "
            "Add 'role: assistant' (primary) or 'role: butler' (voice)."
        )
    if raw in ROLE_ALIASES:
        canonical = ROLE_ALIASES[raw]
        logger.warning(
            "Agent config %s uses deprecated role '%s'; treating as '%s'. "
            "Update the YAML to silence this warning.",
            source,
            raw,
            canonical,
        )
        return canonical
    return raw


def resolve_model(shortname: str) -> str:
    """Resolve a shortname to a full Anthropic model ID.

    If *shortname* is already a full model ID (contains a hyphen and digits),
    it is returned unchanged. Otherwise it must be a key in MODEL_MAP.

    Raises ValueError for unknown shortnames.
    """
    if shortname in MODEL_MAP:
        return MODEL_MAP[shortname]
    # Passthrough for already-full IDs (e.g. "claude-sonnet-4-6")
    if "-" in shortname:
        return shortname
    raise ValueError(
        f"Unknown model shortname '{shortname}'. "
        f"Valid shortnames: {', '.join(MODEL_MAP)}"
    )


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)}")


def _substitute_env(text: str) -> str:
    """Replace ``${VAR_NAME}`` placeholders with environment variable values."""

    def _replacer(match: re.Match) -> str:
        var = match.group(1)
        return os.environ.get(var, match.group(0))

    return _ENV_RE.sub(_replacer, text)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ToolsConfig:
    allowed: list[str] = field(default_factory=list)
    disallowed: list[str] = field(default_factory=list)
    permission_mode: str = ""
    max_turns: int = 10


@dataclass
class MemoryConfig:
    peer_name: str = ""
    token_budget: int = 4000
    exclude_tags: list[str] = field(default_factory=list)


@dataclass
class SessionConfig:
    strategy: str = "ephemeral"
    idle_timeout: int = 300


@dataclass
class AgentConfig:
    name: str = ""
    role: str = ""
    model: str = ""
    personality: str = ""
    description: str = ""
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    mcp_server_names: list[str] = field(default_factory=list)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    channels: list[str] = field(default_factory=list)
    cwd: str = ""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _build_tools_config(raw: dict[str, Any] | None) -> ToolsConfig:
    if not raw:
        return ToolsConfig()
    return ToolsConfig(
        allowed=raw.get("allowed", []),
        disallowed=raw.get("disallowed", []),
        permission_mode=raw.get("permission_mode", ""),
        max_turns=raw.get("max_turns", 10),
    )


def _build_memory_config(raw: dict[str, Any] | None) -> MemoryConfig:
    if not raw:
        return MemoryConfig()
    return MemoryConfig(
        peer_name=raw.get("peer_name", ""),
        token_budget=raw.get("token_budget", 4000),
        exclude_tags=raw.get("exclude_tags", []),
    )


def _build_session_config(raw: dict[str, Any] | None) -> SessionConfig:
    if not raw:
        return SessionConfig()
    return SessionConfig(
        strategy=raw.get("strategy", "ephemeral"),
        idle_timeout=raw.get("idle_timeout", 300),
    )


def load_agent_config(path: str) -> AgentConfig:
    """Load an agent configuration from a YAML file.

    Environment variable placeholders (``${VAR}``) are substituted and
    the ``model`` field is resolved via :func:`resolve_model`.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw_text = fh.read()

    raw_text = _substitute_env(raw_text)
    data: dict[str, Any] = yaml.safe_load(raw_text)

    return AgentConfig(
        name=data.get("name", ""),
        role=_normalize_role(data.get("role", ""), path),
        model=resolve_model(data.get("model", "")),
        personality=data.get("personality", ""),
        description=data.get("description", ""),
        tools=_build_tools_config(data.get("tools")),
        mcp_server_names=data.get("mcp_server_names", []),
        memory=_build_memory_config(data.get("memory")),
        session=_build_session_config(data.get("session")),
        channels=data.get("channels", []),
        cwd=data.get("cwd", ""),
    )
