"""Per-agent-directory loader.

One directory, one file per concern, schema-validated on load. The
composed system prompt is the derived field used by ``Agent._process``
verbatim.
"""

from __future__ import annotations

import json
import os
from typing import Any

import yaml
import jsonschema

from config import (
    AgentConfig,
    CharacterConfig,
    DelegateEntry,
    DisclosureConfig,
    HooksConfig,
    MemoryConfig,
    ResponseShapeConfig,
    SessionConfig,
    ToolsConfig,
    TriggerSpec,
    TTSConfig,
    VoiceConfig,
    resolve_model,
    _substitute_env,
)
from policies import PolicyLibrary, render_disclosure_section

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "defaults", "schema")

# --- Tier file-set rules ---------------------------------------------------

TIER_FILES: dict[str, dict[str, set[str]]] = {
    "resident": {
        "required":  {"character.yaml", "voice.yaml", "response_shape.yaml",
                      "disclosure.yaml", "runtime.yaml"},
        "optional":  {"delegates.yaml", "triggers.yaml", "hooks.yaml"},
        "forbidden": set(),
    },
    "specialist": {
        "required":  {"character.yaml", "voice.yaml", "response_shape.yaml",
                      "runtime.yaml"},
        "optional":  {"hooks.yaml"},
        "forbidden": {"disclosure.yaml", "delegates.yaml", "triggers.yaml"},
    },
}

_DELEGATE_MCP_TOOL = "mcp__casa-framework__delegate_to_specialist"


class LoadError(Exception):
    """Raised on any per-agent load failure."""


# --- Schema cache ----------------------------------------------------------


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _load_schema(name: str) -> dict[str, Any]:
    if name not in _SCHEMA_CACHE:
        path = os.path.join(SCHEMA_DIR, f"{name}.v1.json")
        with open(path, "r", encoding="utf-8") as fh:
            _SCHEMA_CACHE[name] = json.load(fh)
    return _SCHEMA_CACHE[name]


def _validate(data: dict[str, Any], schema_name: str, source: str) -> None:
    schema = _load_schema(schema_name)
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        # Surface the file name and field path for debuggability. The
        # validator's `exc.message` is terse; `exc.absolute_path` names the
        # offending key when available.
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise LoadError(
            f"{source}: schema violation at {where}: {exc.message}"
        ) from exc


# --- File reader -----------------------------------------------------------


def _read_yaml(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    text = _substitute_env(text)
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise LoadError(f"{path}: YAML parse error: {exc}") from exc


def _infer_tier(runtime_data: dict[str, Any]) -> str:
    channels = runtime_data.get("channels") or []
    return "resident" if channels else "specialist"


def _check_file_set(agent_dir: str, tier: str, role: str) -> None:
    rules = TIER_FILES[tier]
    required = rules["required"]
    optional = rules["optional"]
    forbidden = rules["forbidden"]

    on_disk: set[str] = set()
    for entry in os.listdir(agent_dir):
        if entry.startswith("."):
            continue  # dotfiles skipped per spec
        if os.path.isdir(os.path.join(agent_dir, entry)):
            continue  # subdirectories are not config files (e.g. prompts/)
        on_disk.add(entry)

    missing = required - on_disk
    if missing:
        raise LoadError(
            f"agent {role!r} ({tier}): missing required file(s): "
            f"{sorted(missing)}",
        )

    present_forbidden = forbidden & on_disk
    if present_forbidden:
        raise LoadError(
            f"agent {role!r} ({tier}): forbidden file(s) present: "
            f"{sorted(present_forbidden)}",
        )

    allowed = required | optional
    unknown = on_disk - allowed
    if unknown:
        raise LoadError(
            f"agent {role!r} ({tier}): unknown file(s) in directory: "
            f"{sorted(unknown)}",
        )


# --- Section renderers -----------------------------------------------------


def _render_voice_section(voice: VoiceConfig) -> str:
    lines = ["### Voice", ""]
    if voice.tone:
        lines.append("Tone: " + ", ".join(voice.tone))
    lines.append(f"Cadence: {voice.cadence}")
    if voice.forbidden_patterns:
        lines.append("Avoid: " + ", ".join(voice.forbidden_patterns))
    if voice.signature_phrases:
        lines.append("Signature phrases:")
        for k, v in voice.signature_phrases.items():
            lines.append(f"  - {k}: \"{v}\"")
    return "\n".join(lines).rstrip() + "\n"


def _render_response_shape_section(rs: ResponseShapeConfig) -> str:
    lines = ["### Response shape", ""]
    lines.append(f"Register: {rs.register}")
    lines.append(f"Format: {rs.format}")
    lines.append(f"Max sentences (confirmation): {rs.max_sentences_confirmation}")
    lines.append(f"Max sentences (status): {rs.max_sentences_status}")
    if rs.rules:
        lines.append("Rules:")
        for r in rs.rules:
            lines.append(f"  - {r}")
    return "\n".join(lines).rstrip() + "\n"


def _render_delegates_section(delegates: list[DelegateEntry]) -> str:
    if not delegates:
        return ""
    lines = ["### Delegation", ""]
    lines.append("You may delegate to the following specialists:")
    for d in delegates:
        lines.append(f"  - {d.agent}: {d.purpose} — when: {d.when}")
    lines.append("")
    lines.append(
        "Invoke via mcp__casa-framework__delegate_to_specialist(specialist=..., task=...)."
    )
    return "\n".join(lines).rstrip() + "\n"


# --- Per-file builders -----------------------------------------------------


def _resolve_prose(
    data: dict[str, Any],
    *,
    field: str,
    agent_dir: str,
    source_label: str,
) -> str:
    """Return either the inline ``<field>`` string or the contents of the
    markdown file referenced by ``<field>_file`` (relative to agent_dir).

    JSON Schema enforces exactly-one-of, so at runtime we trust that
    constraint; the defensive check here surfaces a clearer error if the
    schema ever drifts. Applies ``_substitute_env`` so external prompts
    see the same env-var substitutions as inline YAML strings.
    """
    inline = data.get(field)
    file_ref = data.get(f"{field}_file")

    if inline is not None and file_ref is not None:
        raise LoadError(
            f"{source_label}: both {field!r} and {field}_file set — "
            f"exactly one must be provided"
        )
    if inline is not None:
        return inline
    if file_ref is None:
        return ""

    if os.path.isabs(file_ref):
        raise LoadError(
            f"{source_label}: {field}_file must be relative to agent dir, "
            f"got {file_ref!r}"
        )

    resolved = os.path.realpath(os.path.join(agent_dir, file_ref))
    agent_dir_abs = os.path.realpath(agent_dir)
    try:
        common = os.path.commonpath([agent_dir_abs, resolved])
    except ValueError:
        common = ""
    if common != agent_dir_abs:
        raise LoadError(
            f"{source_label}: {field}_file {file_ref!r} escapes agent dir"
        )
    if not resolved.endswith(".md"):
        raise LoadError(
            f"{source_label}: {field}_file must end in .md, got {file_ref!r}"
        )
    if not os.path.exists(resolved):
        raise LoadError(
            f"{source_label}: {field}_file not found at {resolved}"
        )

    with open(resolved, "r", encoding="utf-8") as fh:
        return _substitute_env(fh.read())


def _build_character(data: dict[str, Any], *, agent_dir: str) -> CharacterConfig:
    return CharacterConfig(
        name=data["name"],
        archetype=data["archetype"],
        card=_resolve_prose(
            data, field="card", agent_dir=agent_dir,
            source_label="character.yaml",
        ),
        prompt=_resolve_prose(
            data, field="prompt", agent_dir=agent_dir,
            source_label="character.yaml",
        ),
    )


def _build_voice(data: dict[str, Any]) -> VoiceConfig:
    return VoiceConfig(
        tone=list(data.get("tone") or []),
        cadence=data.get("cadence", "natural"),
        forbidden_patterns=list(data.get("forbidden_patterns") or []),
        signature_phrases=dict(data.get("signature_phrases") or {}),
    )


def _build_response_shape(data: dict[str, Any]) -> ResponseShapeConfig:
    return ResponseShapeConfig(
        max_sentences_confirmation=data.get("max_sentences_confirmation", 2),
        max_sentences_status=data.get("max_sentences_status", 3),
        register=data.get("register", "written"),
        format=data.get("format", "plain"),
        rules=list(data.get("rules") or []),
    )


def _build_disclosure(data: dict[str, Any]) -> DisclosureConfig:
    return DisclosureConfig(
        policy=data["policy"],
        overrides=dict(data.get("overrides") or {}),
    )


def _build_delegates(data: dict[str, Any]) -> list[DelegateEntry]:
    return [
        DelegateEntry(agent=e["agent"], purpose=e["purpose"], when=e["when"])
        for e in (data.get("delegates") or [])
    ]


def _build_triggers(
    data: dict[str, Any], *, agent_dir: str,
) -> list[TriggerSpec]:
    specs: list[TriggerSpec] = []
    for t in (data.get("triggers") or []):
        trig_name = t.get("name", "?")
        if t.get("type") in ("interval", "cron"):
            prompt_text = _resolve_prose(
                t, field="prompt", agent_dir=agent_dir,
                source_label=f"triggers.yaml::{trig_name}",
            )
        else:
            prompt_text = ""  # webhook triggers have no prompt
        specs.append(TriggerSpec(
            name=t["name"],
            type=t["type"],
            minutes=int(t.get("minutes", 0) or 0),
            schedule=t.get("schedule", "") or "",
            path=t.get("path", "") or "",
            channel=t.get("channel", "") or "",
            prompt=prompt_text,
        ))
    return specs


def _build_hooks(data: dict[str, Any]) -> HooksConfig:
    return HooksConfig(
        pre_tool_use=list(data.get("pre_tool_use") or []),
    )


def _build_runtime_fields(
    cfg: AgentConfig, runtime: dict[str, Any],
) -> None:
    """Populate the legacy runtime fields on *cfg* from runtime.yaml data."""
    cfg.model = resolve_model(runtime["model"])
    cfg.enabled = bool(runtime.get("enabled", True))

    tools = runtime.get("tools") or {}
    cfg.tools = ToolsConfig(
        allowed=list(tools.get("allowed") or []),
        disallowed=list(tools.get("disallowed") or []),
        permission_mode=tools.get("permission_mode", "acceptEdits"),
        max_turns=int(tools.get("max_turns", 10)),
    )

    cfg.mcp_server_names = list(runtime.get("mcp_server_names") or [])

    memory = runtime.get("memory") or {}
    cfg.memory = MemoryConfig(
        token_budget=int(memory.get("token_budget", 4000)),
        read_strategy=memory.get("read_strategy", "per_turn"),
        scopes_owned=list(memory.get("scopes_owned") or []),
        scopes_readable=list(memory.get("scopes_readable") or []),
        default_scope=memory.get("default_scope", "") or "",
    )

    session = runtime.get("session") or {}
    cfg.session = SessionConfig(
        strategy=session.get("strategy", "ephemeral"),
        idle_timeout=int(session.get("idle_timeout", 300)),
    )

    tts = runtime.get("tts") or {}
    cfg.tts = TTSConfig(tag_dialect=tts.get("tag_dialect", "square_brackets"))

    cfg.voice_errors = dict(runtime.get("voice_errors") or {})
    cfg.channels = list(runtime.get("channels") or [])
    cfg.cwd = runtime.get("cwd", "") or ""


# --- Prompt composer -------------------------------------------------------


def _compose_prompt(
    cfg: AgentConfig, policies: PolicyLibrary | None,
) -> str:
    parts: list[str] = [cfg.character.prompt.rstrip() + "\n"]
    parts.append(_render_voice_section(cfg.voice))
    parts.append(_render_response_shape_section(cfg.response_shape))

    deleg_section = _render_delegates_section(cfg.delegates)
    if deleg_section:
        parts.append(deleg_section)

    if cfg.disclosure is not None:
        if policies is None:
            raise LoadError(
                f"agent {cfg.role!r}: disclosure.yaml references policy "
                f"{cfg.disclosure.policy!r} but no PolicyLibrary was passed"
            )
        resolved = policies.resolve(
            cfg.disclosure.policy, cfg.disclosure.overrides,
        )
        parts.append(render_disclosure_section(resolved))

    return "\n".join(parts).rstrip() + "\n"


# --- Public API ------------------------------------------------------------


def load_agent_from_dir(
    agent_dir: str, *, policies: PolicyLibrary | None,
) -> AgentConfig:
    """Load one agent directory. Strict: every error raises LoadError.

    ``policies`` may be None for specialist loads (specialists have no
    disclosure.yaml). It must be non-None for residents or the composer
    raises at Disclosure-render time.
    """
    if not os.path.isdir(agent_dir):
        raise LoadError(f"not a directory: {agent_dir}")

    role_from_path = os.path.basename(agent_dir.rstrip(os.sep))

    # Peek runtime.yaml first to infer tier before the file-set check.
    runtime_path = os.path.join(agent_dir, "runtime.yaml")
    if not os.path.exists(runtime_path):
        raise LoadError(
            f"agent {role_from_path!r}: missing required file runtime.yaml",
        )
    runtime_data = _read_yaml(runtime_path)
    _validate(runtime_data, "runtime", runtime_path)
    tier = _infer_tier(runtime_data)

    _check_file_set(agent_dir, tier, role_from_path)

    # character.yaml — mandatory, validate then parse.
    char_path = os.path.join(agent_dir, "character.yaml")
    char_data = _read_yaml(char_path)
    _validate(char_data, "character", char_path)
    if char_data["role"] != role_from_path:
        raise LoadError(
            f"agent {role_from_path!r}: character.yaml role "
            f"{char_data['role']!r} must match directory name"
        )

    cfg = AgentConfig(role=role_from_path)
    cfg.character = _build_character(char_data, agent_dir=agent_dir)

    # voice.yaml
    voice_path = os.path.join(agent_dir, "voice.yaml")
    voice_data = _read_yaml(voice_path)
    _validate(voice_data, "voice", voice_path)
    cfg.voice = _build_voice(voice_data)

    # response_shape.yaml
    rs_path = os.path.join(agent_dir, "response_shape.yaml")
    rs_data = _read_yaml(rs_path)
    _validate(rs_data, "response_shape", rs_path)
    cfg.response_shape = _build_response_shape(rs_data)

    # runtime.yaml — already read + validated; build fields.
    _build_runtime_fields(cfg, runtime_data)

    # disclosure.yaml — resident only (file-set check guarantees presence).
    if tier == "resident":
        disc_path = os.path.join(agent_dir, "disclosure.yaml")
        disc_data = _read_yaml(disc_path)
        _validate(disc_data, "disclosure", disc_path)
        cfg.disclosure = _build_disclosure(disc_data)

    # delegates.yaml — optional, resident only.
    deleg_path = os.path.join(agent_dir, "delegates.yaml")
    if os.path.exists(deleg_path):
        deleg_data = _read_yaml(deleg_path)
        _validate(deleg_data, "delegates", deleg_path)
        cfg.delegates = _build_delegates(deleg_data)

    # triggers.yaml — optional, resident only.
    trig_path = os.path.join(agent_dir, "triggers.yaml")
    if os.path.exists(trig_path):
        trig_data = _read_yaml(trig_path)
        _validate(trig_data, "triggers", trig_path)
        cfg.triggers = _build_triggers(trig_data, agent_dir=agent_dir)

    # hooks.yaml — optional.
    hooks_path = os.path.join(agent_dir, "hooks.yaml")
    if os.path.exists(hooks_path):
        hooks_data = _read_yaml(hooks_path)
        _validate(hooks_data, "hooks", hooks_path)
        cfg.hooks = _build_hooks(hooks_data)

    # Delegation-tool invariant: if delegates is non-empty, the MCP
    # tool must be whitelisted by runtime.tools.allowed.
    if cfg.delegates and _DELEGATE_MCP_TOOL not in cfg.tools.allowed:
        raise LoadError(
            f"agent {role_from_path!r}: delegates.yaml is non-empty but "
            f"runtime.yaml tools.allowed is missing "
            f"{_DELEGATE_MCP_TOOL!r}"
        )

    # --- 3.2 scope validation -------------------------------------------
    mem = cfg.memory
    if tier == "resident":
        if mem.scopes_readable:
            # scopes_owned ⊆ scopes_readable
            missing = set(mem.scopes_owned) - set(mem.scopes_readable)
            if missing:
                raise LoadError(
                    f"agent {role_from_path!r}: memory.scopes_owned must be a "
                    f"subset of memory.scopes_readable; missing from readable: "
                    f"{sorted(missing)}"
                )
            # default_scope required and must be in scopes_owned
            if not mem.default_scope:
                raise LoadError(
                    f"agent {role_from_path!r}: memory.default_scope is "
                    f"required when scopes_readable is non-empty"
                )
            if mem.default_scope not in mem.scopes_owned:
                raise LoadError(
                    f"agent {role_from_path!r}: memory.default_scope "
                    f"{mem.default_scope!r} must be in scopes_owned "
                    f"{mem.scopes_owned}"
                )
    else:  # specialist
        if mem.default_scope:
            raise LoadError(
                f"agent {role_from_path!r}: specialist must not declare "
                f"memory.default_scope"
            )
        if mem.scopes_owned or mem.scopes_readable:
            # This is already enforced upstream per 3.1 but re-assert.
            raise LoadError(
                f"agent {role_from_path!r}: specialist must not declare "
                f"memory.scopes_owned or memory.scopes_readable"
            )

    # Compose the system prompt.
    cfg.system_prompt = _compose_prompt(cfg, policies)

    return cfg


def load_all_agents(
    agents_dir: str, *, policies: PolicyLibrary | None,
) -> dict[str, AgentConfig]:
    """Walk *agents_dir* for resident directories.

    Skips ``specialists/`` (Tier 2 home) and any dotdir. Each
    subdirectory's name becomes the agent role. Raises ``LoadError``
    on the first malformed agent — strict-mode from day one.
    """
    found: dict[str, AgentConfig] = {}
    if not os.path.isdir(agents_dir):
        return found
    for entry in sorted(os.listdir(agents_dir)):
        if entry.startswith(".") or entry == "specialists":
            continue
        path = os.path.join(agents_dir, entry)
        if not os.path.isdir(path):
            raise LoadError(
                f"unexpected non-directory at agents/{entry} — each agent "
                f"is a directory; flat YAML files are no longer supported"
            )
        cfg = load_agent_from_dir(path, policies=policies)
        found[cfg.role] = cfg
    return found


def load_all_specialists(
    specialists_dir: str,
) -> dict[str, AgentConfig]:
    """Walk *specialists_dir* for specialist directories.

    Specialists never reference the policy library (taxonomy §4.4: the
    delegating resident owns the disclosure layer).
    """
    found: dict[str, AgentConfig] = {}
    if not os.path.isdir(specialists_dir):
        return found
    for entry in sorted(os.listdir(specialists_dir)):
        if entry.startswith("."):
            continue
        path = os.path.join(specialists_dir, entry)
        if not os.path.isdir(path):
            raise LoadError(
                f"unexpected non-directory at specialists/{entry} — each "
                f"specialist is a directory; flat YAML files are no longer "
                f"supported"
            )
        cfg = load_agent_from_dir(path, policies=None)
        found[cfg.role] = cfg
    return found
