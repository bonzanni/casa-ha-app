"""Casa runtime container — single dataclass holding state previously
held by casa_core.main's closure.

Passed through init_tools(runtime=...) and stashed on the agent module
as `agent.active_runtime` so reload handlers (reload.py) and tool
handlers (tools.py) can reach all the registries without re-plumbing
through every callsite.

Spec: docs/superpowers/specs/2026-05-02-granular-reload-design.md §2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from agent import Agent
    from agent_registry import AgentRegistry
    from bus import MessageBus
    from channels import ChannelManager
    from config import AgentConfig
    from engagement_registry import EngagementRegistry
    from executor_registry import ExecutorRegistry
    from job_registry import JobRegistry
    from mcp_registry import McpServerRegistry
    from personality_binding import BindingRecord
    from persona_pack import PersonaPack
    from policies import PolicyLibrary
    from prompt_compiler import CompiledPromptBundle
    from role_slot import RoleSlot
    from explanation_store import ExplanationStore
    from semantic_memory import SemanticMemory
    from session_registry import SessionRegistry
    from specialist_registry import SpecialistRegistry
    from trigger_registry import TriggerRegistry
    from channels.voice.delivery import VoiceDeliveryCoordinator
    from channels.voice.routes import VoiceRouteRegistry


@dataclass
class CasaRuntime:
    """Container for all process-global Casa state mutated by reloads.

    Mutability contract:
    - ``agents`` and ``role_configs`` are MUTATED by reload handlers
      (atomic-swap of role keys).
    - Registry attrs (``agent_registry``, ``policy_lib``) are
      REPLACED by reload handlers (rebind).
    - Channel/bus/driver attrs are read-only after boot.
    - Path attrs (``config_dir``, ``agents_dir``, ``home_root``,
      ``defaults_root``) are read-only after boot.
    """

    # Mutable role-keyed dicts
    agents: dict[str, "Agent"]
    role_configs: dict[str, "AgentConfig"]

    # Registries (replaced by reloads; never mutated in place)
    specialist_registry: "SpecialistRegistry"
    executor_registry: "ExecutorRegistry"
    engagement_registry: "EngagementRegistry"
    agent_registry: "AgentRegistry"
    trigger_registry: "TriggerRegistry"
    mcp_registry: "McpServerRegistry"
    session_registry: "SessionRegistry"

    # Channels + bus + drivers (boot-fixed)
    channel_manager: "ChannelManager"
    bus: "MessageBus"
    engagement_driver: Any  # InCasaDriver — avoid import cycle
    claude_code_driver: Any  # ClaudeCodeDriver — avoid import cycle

    # Policy (boot-fixed)
    policy_lib: "PolicyLibrary"

    # Paths (boot-fixed)
    config_dir: str
    agents_dir: str
    home_root: str | Path
    defaults_root: str | Path

    # Long-term memory (boot-fixed). H9 (v0.49.0): reload._construct_agent
    # passes this into every Agent it builds — omitting it silently
    # downgraded reload-constructed residents to NoOpSemanticMemory.
    # Defaulted, so it MUST stay the LAST field (dataclass rule); test
    # stand-ins that skip it get None → Agent maps None to NoOp.
    semantic_memory: "SemanticMemory | None" = None

    # Durable delegated execution + delivery state. Defaulted for existing
    # narrow test stand-ins; production always injects the boot-loaded owner.
    job_registry: "JobRegistry | None" = None
    voice_route_registry: "VoiceRouteRegistry | None" = None
    voice_delivery_coordinator: "VoiceDeliveryCoordinator | None" = None

    # Personality Phase A, Task 8: read-only persona/binding registries derived
    # from the loaded resident configs at boot (and rebuilt by reloads). Keyed
    # per the interface note: role_slots by role_id, persona_packs by
    # "<persona_id>@<version>", bindings/compiled_prompt_bundles by role_id.
    # Defaulted (empty) so every existing narrow CasaRuntime(...) test
    # constructor keeps compiling unchanged — MUST stay after the fields above
    # (dataclass-ordering rule).
    role_slots: "Mapping[str, RoleSlot]" = field(default_factory=dict)
    persona_packs: "Mapping[str, PersonaPack]" = field(default_factory=dict)
    bindings: "Mapping[str, BindingRecord]" = field(default_factory=dict)
    compiled_prompt_bundles: "Mapping[str, CompiledPromptBundle]" = field(default_factory=dict)

    # Personality Phase A, Task 14: lean per-correlation-id explanation store
    # (inspect/explain telemetry). Constructed once at boot
    # (ExplanationStore(Path("/data/explanations"))) and preserved verbatim
    # across reload.py's mutate-in-place candidate-registry swap (reload.py
    # never reconstructs CasaRuntime). Defaulted (None) so every existing
    # narrow CasaRuntime(...) test constructor keeps compiling unchanged —
    # MUST stay the final field (dataclass-ordering rule).
    explanation_store: "ExplanationStore | None" = None
