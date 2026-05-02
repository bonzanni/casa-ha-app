"""In-process reload dispatcher and per-scope handlers.

Spec: docs/superpowers/specs/2026-05-02-granular-reload-design.md.

Public API:
- ``dispatch(scope, *, runtime, role=None, include_env=False) -> dict``
  is the single entry point used by both ``tools.casa_reload`` (MCP) and
  the ``/admin/reload`` route (casactl).
- ``ReloadError(kind, message)`` is raised by handlers on failure;
  ``dispatch`` catches and converts to result-shape.

Lock registry: per-scope-key ``asyncio.Lock`` keyed by
``f"{scope}:{role}"`` for role-bearing scopes, ``scope`` alone otherwise.
The ``full`` scope grabs ``"full"`` and is mutually exclusive with all
other scopes via the ``_GLOBAL_LOCK`` mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("reload")


class ReloadError(Exception):
    """Raised by per-scope handlers; converted to result envelope."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# Per-scope-key lock registry. Keys are stable strings:
#   agent:<role>, triggers:<role>, policies, plugin_env, agents, full
_LOCKS: dict[str, asyncio.Lock] = {}

# Global lock — held in EXCLUSIVE mode by ``full``, in SHARED mode by all
# other scopes. Implemented as a Reader-Writer-style asyncio primitive
# below since asyncio.Lock alone is mutex-only.
_GLOBAL_RW = None  # initialized lazily — see _global_rw()


class _RWLock:
    """Minimal async reader-writer lock. Many readers OR one writer.

    Used so the ``full`` scope (writer) excludes every other scope
    (readers), but readers run concurrently for different scope-keys.
    """

    def __init__(self) -> None:
        self._readers = 0
        self._cond = asyncio.Condition()

    async def acquire_read(self) -> None:
        async with self._cond:
            self._readers += 1

    async def release_read(self) -> None:
        async with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    async def acquire_write(self) -> None:
        async with self._cond:
            while self._readers > 0:
                await self._cond.wait()

    async def release_write(self) -> None:
        async with self._cond:
            self._cond.notify_all()


def _global_rw() -> _RWLock:
    global _GLOBAL_RW
    if _GLOBAL_RW is None:
        _GLOBAL_RW = _RWLock()
    return _GLOBAL_RW


def _lock_key(scope: str, role: str | None) -> str:
    if scope in ("agent", "triggers"):
        return f"{scope}:{role or ''}"
    return scope


def _get_lock(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


# Handlers registry — populated by per-scope tasks B.1..B.6.
HandlerFn = Callable[..., Awaitable[list[str]]]
_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(scope: str, fn: HandlerFn) -> None:
    """Used by per-scope handler modules (tests + reload-impl tasks)."""
    _HANDLERS[scope] = fn


async def dispatch(
    scope: str,
    *,
    runtime: Any,
    role: str | None = None,
    include_env: bool = False,
) -> dict:
    """Single entry point. Returns a result-shape dict; never raises."""
    started_ms = time.monotonic() * 1000

    handler = _HANDLERS.get(scope)
    if handler is None:
        return {
            "status": "error",
            "kind": "unknown_scope",
            "message": f"unknown scope: {scope!r}; valid: {sorted(_HANDLERS)}",
            "scope": scope, "role": role,
            "ms": int(time.monotonic() * 1000 - started_ms),
            "actions": [],
        }

    rw = _global_rw()
    if scope == "full":
        await rw.acquire_write()
    else:
        await rw.acquire_read()
    try:
        lock_key = _lock_key(scope, role)
        lock = _get_lock(lock_key)
        async with lock:
            try:
                actions = await handler(
                    runtime, role=role, include_env=include_env,
                ) if scope == "full" else await handler(runtime, role=role)
                ms = int(time.monotonic() * 1000 - started_ms)
                logger.info(
                    "casa_reload scope=%s role=%s ms=%d ok=True actions=%s",
                    scope, role, ms, actions,
                )
                return {
                    "status": "ok", "scope": scope, "role": role,
                    "ms": ms, "actions": actions,
                }
            except ReloadError as exc:
                ms = int(time.monotonic() * 1000 - started_ms)
                logger.warning(
                    "casa_reload scope=%s role=%s ms=%d ok=False kind=%s msg=%s",
                    scope, role, ms, exc.kind, exc.message,
                )
                return {
                    "status": "error", "kind": exc.kind,
                    "message": exc.message, "scope": scope, "role": role,
                    "ms": ms, "actions": [],
                }
            except Exception as exc:  # noqa: BLE001 — surface as error envelope
                ms = int(time.monotonic() * 1000 - started_ms)
                logger.warning(
                    "casa_reload scope=%s role=%s ms=%d ok=False kind=unexpected msg=%s",
                    scope, role, ms, exc,
                    exc_info=True,
                )
                return {
                    "status": "error", "kind": "unexpected",
                    "message": str(exc), "scope": scope, "role": role,
                    "ms": ms, "actions": [],
                }
    finally:
        if scope == "full":
            await rw.release_write()
        else:
            await rw.release_read()


# ---------------------------------------------------------------------------
# Per-scope handlers
# ---------------------------------------------------------------------------

import os
from pathlib import Path


async def reload_triggers(runtime: Any, *, role: str | None = None) -> list[str]:
    """Soft-reload triggers for one role. Ports tools.casa_reload_triggers body
    to the runtime/dispatcher contract; full lineage in spec §3.
    """
    if not role:
        raise ReloadError("role_required", "scope='triggers' requires role")

    if runtime.trigger_registry is None:
        raise ReloadError("not_initialized", "trigger registry not wired")

    # Find the agent dir: residents at agents/<role>/, specialists at
    # agents/specialists/<role>/. Mirrors tools.casa_reload_triggers.
    base = runtime.config_dir
    agents_dir = runtime.agents_dir
    agent_dir: str | None = None
    for candidate in (
        os.path.join(agents_dir, role),
        os.path.join(agents_dir, "specialists", role),
    ):
        if os.path.isdir(candidate):
            agent_dir = candidate
            break
    if agent_dir is None:
        raise ReloadError(
            "unknown_role", f"no agent directory for role={role!r}",
        )

    # H-3 fix carry-forward (v0.34.0): always re-load policies from disk so
    # residents with disclosure.yaml don't trip _compose_prompt's None guard.
    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    import agent_loader
    try:
        cfg = await asyncio.to_thread(
            agent_loader.load_agent_from_dir,
            agent_dir, policies=policy_lib,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", str(exc)) from exc

    try:
        await asyncio.to_thread(
            runtime.trigger_registry.reregister_for,
            role, list(cfg.triggers), list(cfg.channels),
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("reregister_failed", str(exc)) from exc

    # Q-1 fix (v0.35.2): refresh the runtime cache so back-compat
    # consumers (tools.casa_reload_triggers emits `registered=[...]`
    # by reading runtime.role_configs[role].triggers) see the
    # post-reload state, not the boot-time list. Mirrors the resident
    # vs specialist branching of reload_agent at lines 339-348.
    if role in runtime.role_configs:
        runtime.role_configs[role] = cfg
    else:
        try:
            await asyncio.to_thread(runtime.specialist_registry.load)
        except Exception as exc:  # noqa: BLE001
            raise ReloadError("specialist_reload_failed", str(exc)) from exc

    # G-2 hotfix carry-forward: drain pending-reload guard if any.
    try:
        from tools import _ENGAGEMENTS_PENDING_RELOAD, engagement_var
        eng = engagement_var.get(None)
        if eng is not None:
            _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
    except Exception:  # noqa: BLE001 — best-effort
        pass

    return ["reregister_triggers"]


register_handler("triggers", reload_triggers)


def _construct_agent(*, cfg, runtime):
    """Factory wrapper so tests can monkeypatch construction.

    Mirrors the per-role Agent construction in casa_core.main:
    wraps base_memory by strategy (shared logic exists at
    casa_core._wrap_memory_for_strategy — keep using that for parity).
    """
    from agent import Agent
    from casa_core import _wrap_memory_for_strategy
    sqlite_warning_emitted = [False]
    agent_memory = _wrap_memory_for_strategy(
        runtime.base_memory,
        role=cfg.role,
        strategy=cfg.memory.read_strategy,
        sqlite_warning_emitted=sqlite_warning_emitted,
    )
    return Agent(
        config=cfg, memory=agent_memory,
        session_registry=runtime.session_registry,
        mcp_registry=runtime.mcp_registry,
        channel_manager=runtime.channel_manager,
        scope_registry=runtime.scope_registry,
        agent_registry=runtime.agent_registry,
    )


async def reload_agent(runtime: Any, *, role: str | None = None) -> list[str]:
    """Atomic-swap reload of a single role's Agent + AgentConfig.

    Tier detection: residents at agents/<role>/, specialists at
    agents/specialists/<role>/. ``unknown_role`` if neither exists.
    """
    if not role:
        raise ReloadError("role_required", "scope='agent' requires role")

    base = runtime.config_dir
    agents_dir = runtime.agents_dir

    resident_dir = os.path.join(agents_dir, role)
    specialist_dir = os.path.join(agents_dir, "specialists", role)
    if os.path.isdir(resident_dir):
        agent_dir = resident_dir
        tier = "resident"
    elif os.path.isdir(specialist_dir):
        agent_dir = specialist_dir
        tier = "specialist"
    else:
        raise ReloadError(
            "unknown_role", f"no agent directory for role={role!r}",
        )

    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    import agent_loader
    try:
        new_cfg = await asyncio.to_thread(
            agent_loader.load_agent_from_dir, agent_dir, policies=policy_lib,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", str(exc)) from exc

    actions = ["load_config"]

    # Construct new Agent instance OUTSIDE the swap window.
    try:
        new_agent = await asyncio.to_thread(
            _construct_agent, cfg=new_cfg, runtime=runtime,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("construct_failed", str(exc)) from exc
    actions.append("construct_agent")

    # --- ATOMIC SWAP WINDOW ---
    if tier == "resident":
        runtime.role_configs[role] = new_cfg
    else:
        # SpecialistRegistry update — re-scan the dir to refresh in-memory
        # config dict. Mirrors specialist_registry.load() pattern but just
        # for one role.
        try:
            await asyncio.to_thread(runtime.specialist_registry.load)
        except Exception as exc:  # noqa: BLE001
            raise ReloadError("specialist_reload_failed", str(exc)) from exc
    runtime.agents[role] = new_agent
    runtime.bus.register(role, new_agent.handle_message)
    actions.append("reregister_bus")

    # Rebuild agent_registry from current state.
    from agent_registry import AgentRegistry
    runtime.agent_registry = AgentRegistry.build(
        residents=runtime.role_configs,
        specialists=runtime.specialist_registry.all_configs(),
    )
    actions.append("rebuild_agent_registry")

    # Re-register triggers for that role only.
    try:
        await asyncio.to_thread(
            runtime.trigger_registry.reregister_for,
            role, list(new_cfg.triggers), list(new_cfg.channels),
        )
        actions.append("reregister_triggers")
    except Exception as exc:  # noqa: BLE001 — log but don't fail the swap
        logger.warning("trigger reregister failed for role=%s: %s", role, exc)

    # Drain pending-reload guard if any.
    try:
        from tools import _ENGAGEMENTS_PENDING_RELOAD, engagement_var
        eng = engagement_var.get(None)
        if eng is not None:
            _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
    except Exception:  # noqa: BLE001
        pass

    return actions


register_handler("agent", reload_agent)


async def _reload_role_after_policies(runtime: Any, role: str) -> None:
    """Re-load one role's AgentConfig + Agent with the new policy_lib.

    Used by reload_policies — does the agent-scope work without holding
    the agent-scope lock (caller already holds the policies lock; agent
    re-loads here are sequential).
    """
    # Determine tier
    base = runtime.config_dir
    agents_dir = runtime.agents_dir
    resident_dir = os.path.join(agents_dir, role)
    specialist_dir = os.path.join(agents_dir, "specialists", role)
    if os.path.isdir(resident_dir):
        agent_dir = resident_dir
        tier = "resident"
    elif os.path.isdir(specialist_dir):
        agent_dir = specialist_dir
        tier = "specialist"
    else:
        return  # role disappeared between scan and re-load — silently skip

    import agent_loader
    new_cfg = await asyncio.to_thread(
        agent_loader.load_agent_from_dir,
        agent_dir, policies=runtime.policy_lib,
    )
    new_agent = await asyncio.to_thread(
        _construct_agent, cfg=new_cfg, runtime=runtime,
    )
    if tier == "resident":
        runtime.role_configs[role] = new_cfg
    runtime.agents[role] = new_agent
    runtime.bus.register(role, new_agent.handle_message)


async def reload_policies(runtime: Any, *, role: str | None = None) -> list[str]:
    """Reload policies/disclosure.yaml + policies/scopes.yaml; cascade
    to per-role AgentConfig rebuild so agents pick up the new policy_lib.
    """
    base = runtime.config_dir
    actions: list[str] = []

    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        new_policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    import scope_registry as sr_mod
    scopes_path = os.path.join(base, "policies", "scopes.yaml")
    try:
        new_scope_lib = await asyncio.to_thread(
            sr_mod.load_scope_library, scopes_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"scopes: {exc}") from exc

    threshold = float(os.environ.get("CASA_SCOPE_THRESHOLD", "0.35"))
    new_scope_registry = sr_mod.ScopeRegistry(new_scope_lib, threshold=threshold)
    try:
        await new_scope_registry.prepare()
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("embed_error", f"scope embedding: {exc}") from exc

    # Stage swaps in locals; commit to runtime atomically.
    runtime.policy_lib = new_policy_lib
    runtime.scope_registry = new_scope_registry
    actions += ["reload_policy_lib", "rebuild_scope_registry"]

    # Cascade: re-load each role's Agent so new policy_lib propagates.
    role_list = list(runtime.role_configs.keys()) + list(
        runtime.specialist_registry.all_configs().keys()
    )
    for r in role_list:
        try:
            await _reload_role_after_policies(runtime, r)
        except Exception as exc:  # noqa: BLE001 — one role's failure shouldn't kill the rest
            logger.warning("policies cascade: role=%s failed: %s", r, exc)
    actions.append(f"cascaded_to_{len(role_list)}_roles")

    return actions


register_handler("policies", reload_policies)


# Snapshot of last-applied plugin-env keys, used to detect deletions.
_PLUGIN_ENV_LAST_KEYS: set[str] = set()


async def reload_plugin_env(runtime: Any, *, role: str | None = None) -> list[str]:
    """Re-source plugin-env.conf into os.environ.

    Resolves op:// references via secrets_resolver. Computes the diff
    against the last-applied key set and pops any that are now absent.
    """
    global _PLUGIN_ENV_LAST_KEYS
    import plugin_env_conf
    from secrets_resolver import resolve as resolve_secret

    try:
        entries = await asyncio.to_thread(plugin_env_conf.read_entries)
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("read_error", f"plugin-env.conf: {exc}") from exc

    new_keys: set[str] = set(entries.keys())
    actions: list[str] = []

    for var, raw in entries.items():
        try:
            resolved = await asyncio.to_thread(resolve_secret, raw)
        except RuntimeError as exc:
            logger.warning("plugin-env: %s op:// resolution failed: %s", var, exc)
            resolved = raw  # fall through with literal — same as boot path
        os.environ[var] = resolved
    actions.append(f"set_{len(entries)}_vars")

    # Drop keys present last time but absent now.
    dropped = _PLUGIN_ENV_LAST_KEYS - new_keys
    for var in dropped:
        os.environ.pop(var, None)
    if dropped:
        actions.append(f"dropped_{len(dropped)}_vars")

    _PLUGIN_ENV_LAST_KEYS = new_keys
    return actions


register_handler("plugin_env", reload_plugin_env)


async def reload_agents(runtime: Any, *, role: str | None = None) -> list[str]:
    """Scan agents/ for new/deleted residents + agents/specialists/ for
    new/deleted specialists. Add or evict accordingly.
    """
    actions: list[str] = []
    base = runtime.config_dir
    agents_dir = runtime.agents_dir

    import policies as policies_module
    policy_lib_path = os.path.join(base, "policies", "disclosure.yaml")
    try:
        policy_lib = await asyncio.to_thread(
            policies_module.load_policies, policy_lib_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"policies: {exc}") from exc

    import agent_loader
    import agent_home

    # ---- Residents ----
    on_disk_residents = set()
    if os.path.isdir(agents_dir):
        for ent in os.scandir(agents_dir):
            if ent.is_dir() and ent.name not in (
                "specialists", "executors",
            ):
                on_disk_residents.add(ent.name)

    known_residents = set(runtime.role_configs.keys())

    # Add new residents
    for r in on_disk_residents - known_residents:
        try:
            new_cfg = await asyncio.to_thread(
                agent_loader.load_agent_from_dir,
                os.path.join(agents_dir, r),
                policies=policy_lib,
            )
            await asyncio.to_thread(
                agent_home.provision_agent_home,
                role=r,
                home_root=runtime.home_root,
                defaults_root=runtime.defaults_root,
            )
            new_agent = await asyncio.to_thread(
                _construct_agent, cfg=new_cfg, runtime=runtime,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reload_agents: failed to add %s: %s", r, exc)
            continue
        runtime.role_configs[r] = new_cfg
        runtime.agents[r] = new_agent
        runtime.bus.register(r, new_agent.handle_message)
        actions.append(f"added_{r}")

    # Evict deleted residents
    for r in known_residents - on_disk_residents:
        runtime.role_configs.pop(r, None)
        runtime.agents.pop(r, None)
        try:
            runtime.bus.unregister(r)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reload_agents: bus.unregister(%s) failed: %s", r, exc)
        actions.append(f"evicted_{r}")

    # ---- Specialists ----
    specialists_dir = os.path.join(agents_dir, "specialists")
    on_disk_specialists = set()
    if os.path.isdir(specialists_dir):
        for ent in os.scandir(specialists_dir):
            if ent.is_dir():
                on_disk_specialists.add(ent.name)

    # Defer to specialist_registry's own re-scan, then diff.
    try:
        await asyncio.to_thread(runtime.specialist_registry.load)
    except Exception as exc:  # noqa: BLE001
        logger.warning("specialist_registry.load failed: %s", exc)

    known_specialists = set(runtime.specialist_registry.all_configs().keys())

    # New specialists need agent-home + Agent construction; eviction is
    # handled by the registry's own load() (tombstone-tracked).
    for s in on_disk_specialists - set(runtime.agents.keys()):
        cfg = runtime.specialist_registry.all_configs().get(s)
        if cfg is None:
            continue
        try:
            await asyncio.to_thread(
                agent_home.provision_agent_home,
                role=s,
                home_root=runtime.home_root,
                defaults_root=runtime.defaults_root,
            )
            new_agent = await asyncio.to_thread(
                _construct_agent, cfg=cfg, runtime=runtime,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reload_agents: failed to add specialist %s: %s", s, exc)
            continue
        runtime.agents[s] = new_agent
        runtime.bus.register(s, new_agent.handle_message)
        actions.append(f"added_specialist_{s}")

    # Evict missing specialists from runtime.agents (registry already
    # forgot them via its own load()).
    for s in (set(runtime.agents.keys()) & known_residents) - on_disk_residents:
        # No-op — handled in resident block above.
        pass
    for s in set(runtime.agents.keys()) - on_disk_residents - on_disk_specialists:
        runtime.agents.pop(s, None)
        try:
            runtime.bus.unregister(s)
        except Exception:  # noqa: BLE001
            pass
        actions.append(f"evicted_specialist_{s}")

    # Rebuild agent_registry with fresh state.
    from agent_registry import AgentRegistry
    runtime.agent_registry = AgentRegistry.build(
        residents=runtime.role_configs,
        specialists=runtime.specialist_registry.all_configs(),
    )
    actions.append("rebuild_agent_registry")

    return actions


register_handler("agents", reload_agents)


async def reload_full(
    runtime: Any, *, role: str | None = None, include_env: bool = False,
) -> list[str]:
    """Compose policies + agents + per-role agent (+ optional plugin_env).

    Each sub-handler is invoked DIRECTLY (not via dispatch) so a single
    `full`-scope lock guards the whole sequence — sub-handlers don't
    re-enter the dispatcher's lock machinery.
    """
    actions: list[str] = []

    # Policies — full cascade includes per-role re-load.
    sub = await _HANDLERS["policies"](runtime, role=None)
    actions += [f"policies:{a}" for a in sub]

    # Agents — adds/evicts.
    sub = await _HANDLERS["agents"](runtime, role=None)
    actions += [f"agents:{a}" for a in sub]

    # Per-role agent reload — picks up edits to runtime.yaml etc that the
    # policies cascade re-loaded but where downstream wiring (triggers)
    # might still need a fresh pass.
    for r in list(runtime.role_configs.keys()) + list(
        runtime.specialist_registry.all_configs().keys(),
    ):
        sub = await _HANDLERS["agent"](runtime, role=r)
        actions += [f"agent:{r}:{a}" for a in sub]

    if include_env:
        sub = await _HANDLERS["plugin_env"](runtime, role=None)
        actions += [f"plugin_env:{a}" for a in sub]

    return actions


register_handler("full", reload_full)
