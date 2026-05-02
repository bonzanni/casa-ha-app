"""Casa runtime container — single dataclass holding state previously
held by casa_core.main's closure.

Passed through init_tools(runtime=...) and stashed on the agent module
as `agent.active_runtime` so reload handlers (reload.py) and tool
handlers (tools.py) can reach all the registries without re-plumbing
through every callsite.

Spec: docs/superpowers/specs/2026-05-02-granular-reload-design.md §2.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent import Agent
    from agent_registry import AgentRegistry
    from bus import MessageBus
    from channels import ChannelManager
    from config import AgentConfig
    from engagement_registry import EngagementRegistry
    from executor_registry import ExecutorRegistry
    from mcp_registry import McpServerRegistry
    from memory import MemoryProvider
    from policies import PolicyLibrary
    from scope_registry import ScopeRegistry
    from session_registry import SessionRegistry
    from specialist_registry import SpecialistRegistry
    from trigger_registry import TriggerRegistry


@dataclass
class CasaRuntime:
    """Container for all process-global Casa state mutated by reloads.

    Mutability contract:
    - ``agents`` and ``role_configs`` are MUTATED by reload handlers
      (atomic-swap of role keys).
    - Registry attrs (``agent_registry``, ``scope_registry``,
      ``policy_lib``) are REPLACED by reload handlers (rebind).
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
    scope_registry: "ScopeRegistry"
    session_registry: "SessionRegistry"

    # Channels + bus + drivers (boot-fixed)
    channel_manager: "ChannelManager"
    bus: "MessageBus"
    engagement_driver: Any  # InCasaDriver — avoid import cycle
    claude_code_driver: Any  # ClaudeCodeDriver — avoid import cycle
    memory_provider: "MemoryProvider"

    # Policy + base memory (boot-fixed)
    policy_lib: "PolicyLibrary"
    base_memory: "MemoryProvider"

    # Paths (boot-fixed)
    config_dir: str
    agents_dir: str
    home_root: str | Path
    defaults_root: str | Path
