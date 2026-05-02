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
