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
        # M21 (v0.49.0): the writer must hold visible lock state. Pre-fix
        # acquire_write recorded nothing, so readers arriving while a
        # 'full' reload was mid-flight ran concurrently with its
        # multi-step runtime mutation.
        self._writer = False
        self._cond = asyncio.Condition()

    async def acquire_read(self) -> None:
        async with self._cond:
            while self._writer:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self) -> None:
        async with self._cond:
            self._readers -= 1
            if self._readers == 0:
                # notify_all (not notify): readers and a waiting writer
                # share this one condition.
                self._cond.notify_all()

    async def acquire_write(self) -> None:
        async with self._cond:
            while self._writer or self._readers > 0:
                await self._cond.wait()
            self._writer = True

    async def release_write(self) -> None:
        async with self._cond:
            self._writer = False
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


# Release B: reload scopes whose success can change what plugin triggers may
# route (resident channels/triggers, the agent set, or everything) — dispatch
# re-derives the plugin-trigger overlay after these succeed.
_TRIGGER_RECONCILE_SCOPES = frozenset({"triggers", "agent", "agents", "full"})


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
                # Release B: a reload that can change trigger routing inputs
                # (a resident's channels/triggers, the agent set, or the whole
                # runtime) must re-derive the plugin-trigger overlay — e.g. a
                # resident LOSING its webhook channel must unroute the plugin
                # triggers that targeted it. Failure is non-fatal: the reload
                # itself succeeded; the stale overlay heals on the next
                # reconcile (any plugin mutation / reload).
                if scope in _TRIGGER_RECONCILE_SCOPES:
                    try:
                        import trigger_reconcile
                        await trigger_reconcile.reconcile_from_runtime(runtime)
                        actions = [*actions, "plugin_triggers_reconciled"]
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "plugin-trigger reconcile after reload failed",
                            exc_info=True)
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

    # Personality Phase A, Task 14 (round-3 review): restart-to-swap invariant.
    # The load_agent_from_dir above may have committed a STAGED persona swap
    # desired->active on disk, yielding a NEW role_checksum/binding_digest on
    # cfg while the LIVE resident still runs the OLD identity. A trigger reload
    # must NEVER activate that change — only a supervised restart may. So for a
    # RESIDENT whose personality identity moved, refuse the WHOLE operation here
    # BEFORE anything mutates: no reregister_for, no cache write, no specialist
    # reload. The trigger registry, runtime.role_configs, AND the trigger
    # reconciler's view (trigger_reconcile reads role_configs[role].channels to
    # authorize plugin webhook ingress) all stay consistently OLD, so the
    # restart the swap already requires activates everything together — no mixed
    # state. (Round 2 kept the OLD cache but still reregistered the NEW triggers,
    # a half-applied design two reviewers flagged: it left webhook ingress
    # authorized by the stale cached channels while NEW triggers were live, and
    # misreported the registered trigger list.) Raising mirrors reload_agent's
    # direct-path contract; dispatch() converts this ReloadError to a structured
    # error, and reload_full does NOT compose this handler, so nothing cascades
    # through the raise. The on-disk active.yaml commit from load_agent_from_dir
    # may already have happened — idempotent with the mandatory restart's own
    # boot-time reconcile (same note reload_agent carries at ~:598).
    if role in runtime.role_configs and _resident_identity_changed(
        cfg, runtime.role_configs.get(role),
    ):
        logger.warning(
            "reload_triggers(%s): personality identity changed on disk "
            "(role_checksum or binding_digest differs) — refusing the trigger "
            "reload to avoid mixed state; restart required to activate", role,
        )
        raise ReloadError(
            "restart_required",
            f"role={role} personality identity changed; restart via "
            f"casa_restart_supervised to activate (trigger reload refused to "
            f"avoid mixed state)",
        )

    try:
        await asyncio.to_thread(
            runtime.trigger_registry.reregister_for,
            role, list(cfg.triggers), list(cfg.channels),
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("reregister_failed", str(exc)) from exc

    # Q-1 fix (v0.35.2): refresh the runtime cache so back-compat consumers
    # (tools.casa_reload_triggers emits `registered=[...]` by reading
    # runtime.role_configs[role].triggers) see the post-reload state, not the
    # boot-time list. Mirrors the resident vs specialist branching of
    # reload_agent. A resident whose personality identity changed never reaches
    # here — it was refused above — so this unconditional write can never
    # poison the shared restart-to-swap baseline: cfg's identity always matches
    # the live baseline on this path.
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


# Background agent-pool-close tasks (F12). Held here so they aren't
# garbage-collected mid-flight (a bare fire-and-forget create_task with no
# other reference can be swept by the GC before it completes).
_AGENT_CLOSE_TASKS: set[asyncio.Task] = set()


def _invalidate_role_grants(role: str | None) -> None:
    """Purge authorization grants + cancel pending challenges for `role`
    BEFORE its replacement/removed Agent becomes dispatchable (A:§3.3/§3.4,
    r1-B8/r2-B5). A stale grant, or an approved-but-stale challenge whose
    keyboard tap would dispatch a synthetic continuation to an Agent that no
    longer exists (or now runs different code), must never survive a role's
    Agent being swapped or torn down. ``role`` is normalized (a plain reload
    role is already plain; normalize_role is a harmless no-op for it) so
    every purge/cancel call site agrees on ONE shape."""
    if not role:
        return
    from authz_grants import CHALLENGES, GRANTS, normalize_role
    r = normalize_role(role)
    GRANTS.purge_role(r)
    CHALLENGES.cancel_matching(role=r)


def _track_draining(runtime, role, old_agent):
    """Sol #4: record a swapped-out agent's plugin binding on runtime.draining
    so verify can DISCLOSE it as a consumer still on the PREVIOUS artifact while
    its in-flight turn drains (aclose waits on the turn's lock, ≤ drain timeout).
    Returns the entry (to drop on close) or None."""
    if runtime is None or role is None:
        return None
    binding = dict(getattr(old_agent, "active_plugin_binding", {}) or {})
    if not binding:
        return None
    draining = getattr(runtime, "draining", None)
    if not isinstance(draining, list):
        draining = []
        try:
            runtime.draining = draining
        except Exception:  # noqa: BLE001 — SimpleNamespace/Mock stand-ins
            return None
    entry = {"role": role, "binding": binding}
    draining.append(entry)
    return entry


def _drop_draining(runtime, entry) -> None:
    if entry is None or runtime is None:
        return
    draining = getattr(runtime, "draining", None)
    if isinstance(draining, list):
        try:
            draining.remove(entry)
        except ValueError:
            pass


def _schedule_agent_close(old_agent, *, runtime=None, role=None) -> None:
    """Background-drain a replaced/evicted Agent's SDK client pool (F12).

    Background is load-bearing: casa_reload runs as a casa-framework tool
    INSIDE a warm client's turn — a synchronous drain would deadlock on
    that turn's own entry lock. The drain task waits for in-flight turns
    (bounded by the pool's drain timeout) then disconnects.

    Sol #4: when ``runtime``+``role`` are supplied, the draining agent's plugin
    binding is tracked on ``runtime.draining`` for the duration of the drain so
    verify can disclose the still-running old turn (cleared on close).

    Tolerates non-Agent stand-ins used throughout the reload test suite:
    objects with no ``aclose`` at all (``getattr`` default). A real
    ``Agent.aclose`` is always awaitable.
    """
    aclose = getattr(old_agent, "aclose", None)
    if aclose is None:
        return
    entry = _track_draining(runtime, role, old_agent)
    try:
        coro = aclose()
    except Exception:  # noqa: BLE001 — best-effort teardown, never block reload
        logger.warning("agent aclose() raised while scheduling close", exc_info=True)
        _drop_draining(runtime, entry)
        return
    task = asyncio.create_task(coro, name="agent-pool-close")
    _AGENT_CLOSE_TASKS.add(task)

    def _done(t):
        _AGENT_CLOSE_TASKS.discard(t)
        _drop_draining(runtime, entry)

    task.add_done_callback(_done)


def _construct_agent(*, cfg, runtime):
    """Factory wrapper so tests can monkeypatch construction.

    Mirrors the per-role Agent construction in casa_core.main.

    G-2 v0.37.7: idempotently provision the agent-home for ``cfg.role``
    BEFORE constructing the Agent. The Agent's cwd resolves to
    ``/config/agent-home/<role>`` (agent.py:518-521);
    when the configurator creates a new specialist and calls
    ``casa_reload(scope=agent role=<new>)`` (granular per-role scope),
    the agent-home dir wasn't being created — only the scope=agents
    path provisioned it. Idempotent on existing dirs; cheap mkdir.
    """
    import agent_home
    try:
        agent_home.provision_agent_home(
            role=cfg.role,
            home_root=runtime.home_root,
            defaults_root=runtime.defaults_root,
        )
    except Exception as exc:  # noqa: BLE001 — provisioning is best-effort
        # If provisioning fails the Agent will still try to run with a
        # missing home; surface in logs but don't block construction —
        # we preserve the prior failure mode (SDK error) for visibility
        # rather than swallowing the call here.
        logger.warning(
            "provision_agent_home failed for role=%s: %s", cfg.role, exc,
        )

    from agent import Agent
    return Agent(
        config=cfg,
        session_registry=runtime.session_registry,
        mcp_registry=runtime.mcp_registry,
        channel_manager=runtime.channel_manager,
        agent_registry=runtime.agent_registry,
        # H9 (v0.45.0 regression, fixed v0.49.0): reuse the boot-built
        # long-term memory. Omitting this silently downgraded every
        # reload-constructed resident to NoOpSemanticMemory. getattr with
        # None default keeps runtime stand-ins without the field working
        # (Agent maps None → NoOp).
        semantic_memory=getattr(runtime, "semantic_memory", None),
    )


def _start_bus_loop(runtime: Any, role: str) -> None:
    """Ensure ``role`` has a live bus consumer after a ``bus.register``.

    H10 (v0.49.0): boot only spawns ``run_agent_loop`` consumers for
    boot-time roles (casa_core step 13). A role added by reload used to
    get a queue + handler but no consumer, so its messages sat forever.
    ``MessageBus.start_agent_loop`` is idempotent, so calling this after
    every register is safe for existing roles (their running consumer
    is reused).
    """
    try:
        runtime.bus.start_agent_loop(role)
    except Exception as exc:  # noqa: BLE001 — never fail the swap on this
        logger.warning("start_agent_loop(%s) failed: %s", role, exc)


async def _teardown_role(runtime: Any, role: str) -> None:
    """Best-effort full deregistration of an evicted role.

    H11 (v0.49.0): the remove half of the add/remove lifecycle —
    ``bus.unregister`` cancels the role's consumer task and drops its
    queue + handler (the cancellation is awaited so no consumer
    outlives the evict), then ``reregister_for(role, [], [])`` unwinds
    the role's APScheduler jobs, webhook paths, and webhook-allowlist
    names. Pre-fix, eviction called a bus method that did not exist
    (the AttributeError was swallowed) and never touched triggers, so
    'deleted' residents kept consuming and firing as ghost agents until
    the next add-on restart.
    """
    try:
        task = runtime.bus.unregister(role)
        if isinstance(task, asyncio.Task):
            await asyncio.gather(task, return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reload_agents: bus.unregister(%s) failed: %s", role, exc,
        )
    try:
        await asyncio.to_thread(
            runtime.trigger_registry.reregister_for, role, [], [],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reload_agents: trigger deregister(%s) failed: %s", role, exc,
        )


def _resident_identity_changed(new_cfg: Any, live_cfg: Any) -> bool:
    """True iff a resident's personality identity moved between the live config
    and a freshly-loaded one — i.e. its ``role_checksum`` OR ``binding_digest``
    differs (a role.yaml/doctrine edit, or a staged persona swap/reset that
    ``load_agent_from_dir``'s reconcile committed desired->active).

    Personality Phase A, Task 8/Task 14: this is the ONE canonical restart-to-swap
    predicate, shared by every reload path that could otherwise hot-swap a live
    resident (reload_agent, the policies cascade, the bulk agents sweep). A
    ``None`` ``live_cfg`` (a fresh add, or a non-resident with no live entry) is
    NOT a change -> False. ``getattr`` defaults keep it inert for non-resident
    tiers and narrow test stand-ins whose cfgs carry neither field.

    ``runtime.role_configs`` mutation audit (the comparison BASELINE this
    predicate reads — every production writer must either be identity-guarded
    or provably not an activation path, else a staged identity change can be
    laundered through a poisoned baseline):
      * reload_agent's post-guard commit — guarded (raises restart_required
        first).
      * the policies-cascade swap (_reload_role_after_policies) — guarded
        (skip+warn).
      * the bulk agents-sweep ADD loop — iterates on_disk - known only (a live
        resident never enters it) + guarded as defense-in-depth.
      * reload_triggers' Q-1 cache refresh — refuses outright (raises
        restart_required BEFORE any mutation) on an identity change.
      * the bulk sweep's EVICT ``role_configs.pop(...)`` — deletion-only
        (removes the baseline entry with its agent; never installs a new
        digest), not an activation path, no guard needed.
      * boot-time construction in casa_core — no live baseline exists yet.
    Any NEW writer must be classified against this list."""
    if live_cfg is None:
        return False
    return (
        getattr(new_cfg, "role_checksum", None)
        != getattr(live_cfg, "role_checksum", None)
        or getattr(new_cfg, "binding_digest", None)
        != getattr(live_cfg, "binding_digest", None)
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

    # Personality Phase A, Task 8, Step 9: refuse a hot-swap across a
    # personality-identity change. A resident whose role_checksum OR
    # binding_digest moved (a role.yaml/doctrine edit, or a staged persona
    # swap/reset) is restart-to-swap, never hot-reloaded — the compiled prompt
    # bundle and session epoch are bound to that identity. Runs BEFORE
    # _construct_agent, so a rejected reload wastes no work and leaves every
    # live registry/agent untouched (read-only on this path). getattr defaults
    # keep this inert for non-resident tiers and narrow test stand-ins whose
    # cfgs carry neither field.
    if tier == "resident" and _resident_identity_changed(
        new_cfg, runtime.role_configs.get(role),
    ):
        logger.warning(
            "reload_agent(%s): personality identity changed (role_checksum or "
            "binding_digest differs) — refusing hot-swap, restart required", role,
        )
        # Note: the load_agent_from_dir() call above may have already committed a
        # staged desired->active binding to DISK via reconcile before this guard
        # fires — harmless, since that's idempotent with the mandatory restart's
        # own boot-time reconcile and this guard leaves the live in-memory
        # agent/registries untouched either way.
        raise ReloadError(
            "restart_required",
            f"role={role} personality identity changed; restart via "
            f"casa_restart_supervised to activate",
        )

    # v0.74.1 (Sol B1, live proxy-drive finding): a DISABLED specialist must
    # not be constructed or (re)registered. Reload used to install it into
    # runtime.agents + register its bus handler, leaving it reachable via
    # /invoke — and because the AgentRegistry excludes disabled specialists,
    # its resolve tier-missed to resident:<role> and it would execute with an
    # EMPTY plugin binding. Tear down any existing instance and deregister
    # the role instead; verify reports its plugin targets state="disabled".
    if tier == "specialist" and getattr(new_cfg, "enabled", True) is False:
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE teardown
        # proceeds — the role is about to become undispatchable entirely.
        _invalidate_role_grants(role)
        old_agent = runtime.agents.pop(role, None)
        _schedule_agent_close(old_agent, runtime=runtime, role=role)
        await _teardown_role(runtime, role)
        try:
            await asyncio.to_thread(runtime.specialist_registry.load)
        except Exception as exc:  # noqa: BLE001
            raise ReloadError("specialist_reload_failed", str(exc)) from exc
        from agent_registry import AgentRegistry
        runtime.agent_registry = AgentRegistry.build(
            residents=runtime.role_configs,
            specialists=runtime.specialist_registry.all_configs(),
        )
        actions += ["teardown_disabled_specialist", "rebuild_agent_registry"]
        try:
            from tools import sync_agent_role_map
            sync_agent_role_map(runtime)
            actions.append("refresh_role_map")
        except Exception as exc:  # noqa: BLE001 — log but don't fail
            logger.warning("role-map refresh failed for role=%s: %s",
                           role, exc)
        return actions

    # Construct new Agent instance OUTSIDE the swap window.
    try:
        new_agent = await asyncio.to_thread(
            _construct_agent, cfg=new_cfg, runtime=runtime,
        )
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("construct_failed", str(exc)) from exc
    actions.append("construct_agent")

    # --- ATOMIC SWAP WINDOW ---
    old_agent = runtime.agents.get(role)  # AR-7: capture before overwrite
    # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
    # replacement agent becomes dispatchable.
    _invalidate_role_grants(role)
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
    # H10: a role whose dir was created after boot has no consumer yet;
    # idempotent no-op for roles that already have one.
    _start_bus_loop(runtime, role)
    actions.append("reregister_bus")

    # F12: drain/close the replaced Agent's SDK client pool in the
    # background so no warm subprocess outlives this swap. Sol #4: track its
    # binding on runtime.draining so verify discloses the still-draining turn.
    _schedule_agent_close(old_agent, runtime=runtime, role=role)

    # Rebuild agent_registry from current state.
    from agent_registry import AgentRegistry
    runtime.agent_registry = AgentRegistry.build(
        residents=runtime.role_configs,
        specialists=runtime.specialist_registry.all_configs(),
    )
    actions.append("rebuild_agent_registry")

    # P-6: refresh tools' delegation role map. It is a boot-time snapshot;
    # without this, delegate_to_agent keeps resolving the PRE-reload
    # AgentConfig (stale tools.allowed etc.) for every fresh delegation.
    try:
        from tools import sync_agent_role_map
        sync_agent_role_map(runtime)
        actions.append("refresh_role_map")
    except Exception as exc:  # noqa: BLE001 — log but don't fail the swap
        logger.warning("role-map refresh failed for role=%s: %s", role, exc)

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

    # Personality Phase A, Task 14 (whole-branch review): restart-to-swap must
    # hold on the POLICY cascade too. A resident whose role_checksum OR
    # binding_digest moved (a doctrine edit, or a staged persona swap/reset that
    # the load above committed desired->active on disk) is restart-to-swap —
    # never hot-reloaded. Unlike reload_agent's single-role path we do NOT raise
    # (that would abort the whole cascade for every OTHER role); we SKIP just
    # this role, leaving its LIVE agent + cfg + registries untouched, so its
    # deferred policy change lands only on the mandatory supervised restart. The
    # on-disk desired->active commit is harmless — idempotent with that
    # restart's own boot-time reconcile. Every identity-UNCHANGED role still
    # reloads below to pick up the new policy_lib.
    if tier == "resident" and _resident_identity_changed(
        new_cfg, runtime.role_configs.get(role),
    ):
        logger.warning(
            "policies cascade: role=%s personality identity changed "
            "(role_checksum or binding_digest differs) — skipping hot-swap; the "
            "policy change activates on a supervised restart", role,
        )
        return

    new_agent = await asyncio.to_thread(
        _construct_agent, cfg=new_cfg, runtime=runtime,
    )
    old_agent = runtime.agents.get(role)  # AR-7: capture before overwrite
    # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
    # replacement agent becomes dispatchable.
    _invalidate_role_grants(role)
    if tier == "resident":
        runtime.role_configs[role] = new_cfg
    runtime.agents[role] = new_agent
    runtime.bus.register(role, new_agent.handle_message)
    _start_bus_loop(runtime, role)
    # F12: drain/close the replaced Agent's SDK client pool in the
    # background so no warm subprocess outlives this swap.
    _schedule_agent_close(old_agent)


async def reload_policies(runtime: Any, *, role: str | None = None) -> list[str]:
    """Reload policies/disclosure.yaml; cascade to per-role AgentConfig
    rebuild so agents pick up the new policy_lib.
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

    # Stage swaps in locals; commit to runtime atomically.
    runtime.policy_lib = new_policy_lib
    actions += ["reload_policy_lib"]

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


def note_boot_plugin_env(keys: set[str]) -> None:
    """Seed the last-applied plugin-env key snapshot from the boot path.

    M22 (v0.49.0): casa_core.main step 1b sources plugin-env.conf into
    os.environ directly. Without this seed the snapshot starts empty, so
    the FIRST ``casa_reload(scope='plugin_env')`` computes
    ``dropped = {} - new_keys`` and can never remove a key that was
    applied at boot but has since been deleted from plugin-env.conf —
    a revoked plugin secret survived in the process env (and kept being
    inherited by plugin MCP subprocesses) for the container's lifetime.
    Only the boot path may call this: it alone knows which env vars came
    from plugin-env.conf rather than the ambient environment.
    """
    global _PLUGIN_ENV_LAST_KEYS
    _PLUGIN_ENV_LAST_KEYS = set(keys)


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

    # P4b (2026-07-18 self-containment plan): regenerate plugin health from
    # the NEW effective environment. Without this, a secrets-only repair
    # (set_plugin_env_reference + this reload) could never clear a stale-red
    # plugin-health.json — health regeneration only ran on §3.9 registry
    # mutations. Env refresh stays the primary contract: a health failure
    # logs and is dropped, never turning a successful reload into an error.
    try:
        import tools as tools_mod
        # Sol r4-2: serialize with §3.9 registry mutations — both paths do
        # regenerate → notify → mark_notified against shared state; unlocked
        # interleaving allows stale-red last-writer-wins and a mark_notified
        # race that suppresses a later genuine notification. (Mutations hold
        # this same lock for their whole sequence; no mutation dispatches
        # scope=plugin_env while holding it, so this cannot deadlock.)
        async with tools_mod._PLUGIN_TOOLS_LOCK:
            await asyncio.to_thread(tools_mod._regenerate_plugin_health, [])
            await tools_mod._notify_plugin_health_if_possible()
        actions.append("plugin_health_regenerated")
    except Exception:  # noqa: BLE001
        logger.warning("plugin_env reload: health regeneration failed",
                       exc_info=True)
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
            # Personality Phase A, Task 14 (whole-branch review): enforce
            # restart-to-swap on the bulk sweep with the ONE canonical
            # predicate. This loop only adds genuinely-new residents (r is not
            # in role_configs), so ``live_cfg`` is None and the predicate is
            # False — a fresh add's first activation is legitimate. An identity
            # change on an ALREADY-LIVE resident is never reconstructed here (a
            # known resident is not in this add set) and stays restart-to-swap;
            # sharing the predicate guarantees a live resident can never be
            # hot-swapped onto a new binding via scope=agents even if this path
            # is later broadened to refresh existing residents.
            if _resident_identity_changed(new_cfg, runtime.role_configs.get(r)):
                logger.warning(
                    "reload_agents: role=%s personality identity changed "
                    "(role_checksum or binding_digest differs) — leaving the "
                    "live agent in place; restart required to activate", r,
                )
                continue
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
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
        # (re)constructed agent becomes dispatchable.
        _invalidate_role_grants(r)
        runtime.role_configs[r] = new_cfg
        runtime.agents[r] = new_agent
        runtime.bus.register(r, new_agent.handle_message)
        # H10: without a consumer the new resident's queue is write-only
        # until the next add-on restart.
        _start_bus_loop(runtime, r)
        actions.append(f"added_{r}")

    # Evict deleted residents — H11: full lifecycle teardown (cancel
    # consumer, drop queue/handler, unwind triggers), mirroring the add
    # path's register + start.
    for r in known_residents - on_disk_residents:
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE teardown —
        # the role is about to become undispatchable entirely.
        _invalidate_role_grants(r)
        # Deletion-only baseline write: eviction, not activation — see the
        # role_configs mutation audit on _resident_identity_changed.
        runtime.role_configs.pop(r, None)
        old_agent = runtime.agents.pop(r, None)  # AR-7: capture before drop
        _schedule_agent_close(old_agent)  # F12
        await _teardown_role(runtime, r)
        actions.append(f"evicted_{r}")

    # ---- Specialists ----
    specialists_dir = os.path.join(agents_dir, "specialists")
    on_disk_specialists = set()
    if os.path.isdir(specialists_dir):
        for ent in os.scandir(specialists_dir):
            if ent.is_dir():
                on_disk_specialists.add(ent.name)

    # Defer to specialist_registry's own re-scan, then diff. S-3 (block-S
    # live finding 2026-07-15, N150 07:49:56Z): the add/evict REPORT is a
    # before/after diff of the REGISTRY, not of runtime.agents — boot never
    # puts specialists into runtime.agents (they are direct-loaded), so the
    # first agents reload after boot used to mis-report every boot-loaded
    # specialist as `added_specialist_<role>`. The runtime.agents backfill
    # below still runs for registry-known specialists missing an Agent
    # object (plugin-verify grades specialists through runtime.agents) —
    # it just no longer drives the report.
    known_specialists_before = set(
        runtime.specialist_registry.all_configs().keys())
    try:
        await asyncio.to_thread(runtime.specialist_registry.load)
    except Exception as exc:  # noqa: BLE001
        logger.warning("specialist_registry.load failed: %s", exc)

    # O-2b (v0.37.9): surface per-specialist load failures so casactl
    # callers see them. The registry's load() catches per-dir LoadError
    # internally to keep siblings loading; without surfacing here a
    # malformed new specialist would return ok=True with no trace in
    # the action trail.
    try:
        for name, err in runtime.specialist_registry.load_failures():
            actions.append(f"failed:{name}:{err}")
    except AttributeError:
        # Pre-v0.37.9 registry mock without load_failures(); legacy path.
        pass

    known_specialists_after = set(
        runtime.specialist_registry.all_configs().keys())

    # S-3: the report comes from the registry before/after diff exclusively.
    # An added specialist is delegatable the moment the registry re-scan
    # picked it up (direct-load), independent of whether the Agent-object
    # backfill below succeeds — so the diff, not the backfill, is the truth.
    for s in sorted(known_specialists_after - known_specialists_before):
        actions.append(f"added_specialist_{s}")
    evicted_from_registry = known_specialists_before - known_specialists_after
    for s in sorted(evicted_from_registry):
        actions.append(f"evicted_specialist_{s}")

    # Registry-known specialists missing an Agent object need agent-home +
    # Agent construction (boot direct-loads them without one); eviction is
    # handled by the registry's own load() (tombstone-tracked). Reporting
    # already happened above — this loop is state backfill only.
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
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE the
        # (re)constructed specialist becomes dispatchable.
        _invalidate_role_grants(s)
        runtime.agents[s] = new_agent
        runtime.bus.register(s, new_agent.handle_message)
        _start_bus_loop(runtime, s)

    # Evict missing specialists from runtime.agents (registry already
    # forgot them via its own load()).
    for s in (set(runtime.agents.keys()) & known_residents) - on_disk_residents:
        # No-op — handled in resident block above.
        pass
    for s in set(runtime.agents.keys()) - on_disk_residents - on_disk_specialists:
        # A:§3.3/§3.4 (r2-B5 enumerated seam): purge+cancel BEFORE teardown —
        # the role is about to become undispatchable entirely.
        _invalidate_role_grants(s)
        old_agent = runtime.agents.pop(s, None)  # AR-7: capture before drop
        _schedule_agent_close(old_agent)  # F12
        await _teardown_role(runtime, s)
        # S-3: the registry diff above already reported registry-known
        # evictions; only a runtime.agents entry the diff did NOT cover
        # still needs surfacing here (a leaked entry, or the second step of
        # a disabled-then-deleted specialist: the registry entry went in an
        # earlier reload, the backfilled runtime Agent only now — each run
        # reports the layer it actually tore down).
        if s not in evicted_from_registry:
            actions.append(f"evicted_specialist_{s}")

    # Rebuild agent_registry with fresh state.
    from agent_registry import AgentRegistry
    runtime.agent_registry = AgentRegistry.build(
        residents=runtime.role_configs,
        specialists=runtime.specialist_registry.all_configs(),
    )
    actions.append("rebuild_agent_registry")

    # P-6: refresh tools' delegation role map (adds + evictions included) —
    # same rationale as the reload_agent hook.
    try:
        from tools import sync_agent_role_map
        sync_agent_role_map(runtime)
        actions.append("refresh_role_map")
    except Exception as exc:  # noqa: BLE001 — log but don't fail the sweep
        logger.warning("role-map refresh failed in agents sweep: %s", exc)

    return actions


register_handler("agents", reload_agents)


async def reload_executors(
    runtime: Any, *, role: str | None = None,
) -> list[str]:
    """v0.37.1 A-1: re-scan executors/ and rebuild ExecutorRegistry.

    Picks up adds, deletes, enabled-flag flips, permission_mode
    changes, allowed_tools edits, prompt path changes — anything
    that lives in the executor's `definition.yaml` or its sibling
    files. ~30ms in steady state.

    Closes the v0.35.0+ contract gap where executor lifecycle
    changes required a Supervisor restart.

    O-2a (v0.37.9): residents cache their `<executors>` system-prompt
    block (rendered from ``self.config.executors`` at construct_agent
    time). Re-running the registry load alone leaves residents holding
    stale prompts until the next agent-scope reload. Fan out to
    ``reload_agent`` per resident so the cached state regenerates.
    Specialists are NOT in the fan-out — they don't see executors.
    Per-resident sub-actions are surfaced with prefix ``agent:<role>:``
    so casactl output makes the cascade visible.
    """
    try:
        await asyncio.to_thread(runtime.executor_registry.load)
    except Exception as exc:  # noqa: BLE001
        raise ReloadError("load_error", f"executors: {exc}") from exc
    actions: list[str] = ["rebuild_executor_registry"]

    for r in list(runtime.role_configs.keys()):
        try:
            sub = await _HANDLERS["agent"](runtime, role=r)
            actions += [f"agent:{r}:{a}" for a in sub]
        except ReloadError as exc:
            actions.append(f"agent:{r}:failed:{exc.kind}:{exc.message}")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"agent:{r}:failed:{exc}")

    # v0.71.1 (Sol Task-5): an executor enable/disable flip changes plugin
    # authorization (a disabled executor is dormant; enabling it makes its
    # grant checks real). Refresh plugin-health from the rebuilt registry so
    # enabling an executor whose assigned plugin lacks a grant surfaces
    # authorization_missing + a DM now, instead of leaving the report
    # stale-green until an unrelated regeneration trigger. Never fail the
    # reload on the refresh.
    try:
        from tools import (_notify_plugin_health_if_possible,
                           _regenerate_plugin_health)
        await asyncio.to_thread(_regenerate_plugin_health, [])
        await _notify_plugin_health_if_possible()
        actions.append("plugin_health_regenerated")
    except Exception as exc:  # noqa: BLE001
        logger.debug("executors reload: plugin-health regen skipped: %s", exc)

    return actions


register_handler("executors", reload_executors)


async def reload_config_sync(runtime: Any, *, role: str | None = None) -> list[str]:
    """Re-run the default-sync reconciler live (same entry as boot), then
    cascade agents + policies reloads so synced files take effect without a
    container restart. Spec: 2026-06-08-config-sync-reconciler-design.md §3.1.
    """
    import config_sync

    config_dir = runtime.config_dir
    defaults_dir = getattr(runtime, "defaults_dir", "/opt/casa/defaults")
    data_dir = getattr(runtime, "data_dir", "/data")
    image_version = getattr(runtime, "image_version", "unknown")

    actions: list[str] = []
    rc = await asyncio.to_thread(
        config_sync.run,
        defaults_dir=defaults_dir,
        config_dir=config_dir,
        baseline_dir=os.path.join(data_dir, "config-baseline"),
        report_path=os.path.join(data_dir, "config-sync-report.json"),
        image_version=image_version,
    )
    actions.append(f"reconcile_rc={rc}")

    # Cascade so live runtime picks up any synced changes.
    for scope in ("agents", "policies"):
        handler = _HANDLERS.get(scope)
        if handler is None:
            continue
        try:
            sub = await handler(runtime, role=None)
            actions.append(f"{scope}:{sub}")
        except Exception as exc:  # noqa: BLE001 — one cascade failure shouldn't abort the rest
            logger.warning("config_sync cascade: scope=%s failed: %s", scope, exc)

    return actions


register_handler("config_sync", reload_config_sync)


async def reload_full(
    runtime: Any, *, role: str | None = None, include_env: bool = False,
) -> list[str]:
    """Compose policies + agents + executors + per-role agent
    (+ optional plugin_env).

    Each sub-handler is invoked DIRECTLY (not via dispatch) so a
    single ``full``-scope lock guards the whole sequence —
    sub-handlers don't re-enter the dispatcher's lock machinery.

    Order rationale: executors before per-role agent reload because
    ``engage_executor`` lookups go through the ExecutorRegistry; if
    an operator edits an executor definition and a resident
    delegate-list at the same time, we want the executor refresh to
    land first so any subsequent delegate is dispatching against
    fresh state.
    """
    actions: list[str] = []

    # §3.9 mutation sequencing / manual-edit seam: refresh the plugin resolver
    # snapshot from disk FIRST — BEFORE any agent is reconstructed below — so
    # reconstructed agents pick up the new registry and desired==active
    # verification compares fresh state (a stale snapshot would false-pass).
    import plugin_registry
    await asyncio.to_thread(plugin_registry.reload_snapshot)
    actions.append("plugins:snapshot_reloaded")

    # Policies — full cascade includes per-role re-load.
    sub = await _HANDLERS["policies"](runtime, role=None)
    actions += [f"policies:{a}" for a in sub]

    # Agents — adds/evicts residents + specialists.
    sub = await _HANDLERS["agents"](runtime, role=None)
    actions += [f"agents:{a}" for a in sub]

    # v0.37.1 A-1: executors — picks up definition.yaml edits + adds/deletes.
    sub = await _HANDLERS["executors"](runtime, role=None)
    actions += [f"executors:{a}" for a in sub]

    # Per-role agent reload.
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
