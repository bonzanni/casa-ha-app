"""Configuration dataclasses and model mapping for Casa agents.

The actual loader lives in ``agent_loader.load_agent_from_dir`` — this
module only defines the dataclasses, ``MODEL_MAP`` / ``resolve_model``,
and the ``${ENV}`` substitution helper used by the loader.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def resolve_model(shortname: str) -> str:
    """Resolve a shortname to a full Anthropic model ID.

    If *shortname* is already a full model ID (contains a hyphen and digits),
    it is returned unchanged. Otherwise it must be a key in MODEL_MAP.

    Raises ValueError for unknown shortnames.
    """
    if shortname in MODEL_MAP:
        return MODEL_MAP[shortname]
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


_VALID_READ_STRATEGIES = ("per_turn", "cached", "card_only")


@dataclass
class MemoryConfig:
    token_budget: int = 4000
    read_strategy: str = "per_turn"
    scopes_owned: list[str] = field(default_factory=list)
    scopes_readable: list[str] = field(default_factory=list)


@dataclass
class SessionConfig:
    strategy: str = "ephemeral"
    idle_timeout: int = 300


_VALID_TAG_DIALECTS = ("square_brackets", "parens", "none")


@dataclass
class TTSConfig:
    tag_dialect: str = "square_brackets"

    def __post_init__(self) -> None:
        if self.tag_dialect not in _VALID_TAG_DIALECTS:
            raise ValueError(
                f"Invalid tts.tag_dialect {self.tag_dialect!r}; "
                f"must be one of {_VALID_TAG_DIALECTS}"
            )


@dataclass
class CharacterConfig:
    name: str = ""
    archetype: str = ""
    card: str = ""
    prompt: str = ""


@dataclass
class VoiceConfig:
    tone: list[str] = field(default_factory=list)
    cadence: str = "natural"
    forbidden_patterns: list[str] = field(default_factory=list)
    signature_phrases: dict[str, str] = field(default_factory=dict)


@dataclass
class ResponseShapeConfig:
    max_sentences_confirmation: int = 2
    max_sentences_status: int = 3
    register: str = "written"
    format: str = "plain"
    rules: list[str] = field(default_factory=list)


@dataclass
class DisclosureConfig:
    policy: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class DelegateEntry:
    agent: str
    purpose: str
    when: str


@dataclass
class TriggerSpec:
    name: str
    type: str                                   # interval | cron | webhook
    minutes: int = 0
    schedule: str = ""
    path: str = ""
    channel: str = ""
    prompt: str = ""


@dataclass
class HooksConfig:
    pre_tool_use: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentConfig:
    role: str = ""
    model: str = ""
    enabled: bool = True
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    mcp_server_names: list[str] = field(default_factory=list)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    voice_errors: dict[str, str] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    cwd: str = ""

    character: CharacterConfig = field(default_factory=CharacterConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    response_shape: ResponseShapeConfig = field(default_factory=ResponseShapeConfig)
    disclosure: DisclosureConfig | None = None
    delegates: list[DelegateEntry] = field(default_factory=list)
    triggers: list[TriggerSpec] = field(default_factory=list)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    system_prompt: str = ""
