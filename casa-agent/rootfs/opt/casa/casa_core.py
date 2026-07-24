"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

# Ensure the Casa package root is on sys.path regardless of cwd
_CASA_ROOT = str(Path(__file__).resolve().parent)
if _CASA_ROOT not in sys.path:
    sys.path.insert(0, _CASA_ROOT)

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent_loader import load_all_agents
from authz_grants import CHALLENGES, GRANTS
from bus import BusMessage, MessageBus, MessageType
from channel_authz import agent_allowed_on
from channels import ChannelManager
from claude_runtime import (
    CLAUDE_CLI_PATH,
    CLAUDE_CLI_VERSION,
    verify_effective_cli,
)
from config import AgentConfig
from config_git import init_repo, snapshot_manual_edits
from freshness_reaper import FreshnessReaper
from log_cid import install_logging, new_cid
from casa_core_middleware import cid_middleware, CasaAccessLogger
from ha_mcp_facade import HomeAssistantFacade
from mcp_registry import McpServerRegistry
from semantic_memory import SemanticMemory
from policies import load_policies
from provenance import sanitize_external_context
from session_registry import SessionRegistry
from session_sweeper import SessionSweeper
from rate_limit import RateLimiter, rate_limit_response
from timekeeping import resolve_tz
from trigger_registry import TriggerRegistry
from voice_delivery_config import load_voice_delivery_config

logger = logging.getLogger(__name__)

CONFIG_DIR = "/config"
DATA_DIR = "/data"


# ---------------------------------------------------------------------------
# Plan 4b/3.6: internal Unix-socket AppRunner for svc-casa-mcp consumption
# ---------------------------------------------------------------------------


async def start_internal_unix_runner(
    *,
    socket_path: str,
    tool_dispatch: dict,
    engagement_registry,
    hook_policies: dict,
    executor_hook_policies: dict | None = None,
    runtime=None,
    telegram_channel=None,
) -> "web.AppRunner":
    """Build and start a second aiohttp AppRunner bound to a Unix socket.

    Routes:
      POST /internal/tools/call    -> _make_internal_tools_call_handler(...)
      POST /internal/hooks/resolve -> _make_internal_hooks_resolve_handler(...)
      POST /admin/reload           -> build_admin_reload_handler(...)
        (Task E.1 -- casactl operator CLI dispatch)
      POST /internal/channel/*     -> channels.channel_handlers._make_channel_handlers(...)
        (E-12 v0.37.0 -- only registered when ``telegram_channel`` is not None;
         Phase 1 exposes just /internal/channel/send_to_topic. Tests and any
         fallback boot path without telegram skip this family entirely, so a
         POST to /internal/channel/* on those runners returns 404.)

    Returns the AppRunner so the caller can `await runner.cleanup()` on
    shutdown. We register an `on_cleanup` hook on the internal app that
    unlinks the socket file when cleanup runs — `web.UnixSite` does not
    do this on its own.

    Parent directory permissions: 0700 if we have to create it. Socket
    permissions: 0600 (root-only). Both processes in the addon container
    run as root, so 0600 is sufficient (no group access needed).
    """
    parent = os.path.dirname(socket_path) or "/"
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    # If a prior instance left a stale socket file, remove it.
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError as exc:
            logger.warning(
                "start_internal_unix_runner: stale socket %s could not be "
                "unlinked: %s", socket_path, exc,
            )

    from internal_handlers import (
        _make_internal_tools_call_handler,
        _make_internal_hooks_resolve_handler,
        build_admin_reload_handler,
    )

    internal_app = web.Application()
    internal_app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch=tool_dispatch,
            engagement_registry=engagement_registry,
        ),
    )
    internal_app.router.add_post(
        "/internal/hooks/resolve",
        _make_internal_hooks_resolve_handler(
            hook_policies=hook_policies,
            executor_hook_policies=executor_hook_policies,
            engagement_registry=engagement_registry,
        ),
    )
    # Task E.1 (granular-reload plan): casactl operator CLI POSTs here
    # over the unix socket. Same dispatch path as the casa_reload MCP tool.
    internal_app.router.add_post(
        "/admin/reload",
        build_admin_reload_handler(runtime=runtime),
    )

    # Task 14 (personality Phase A): lean inspection/explain admin routes —
    # POST /admin/personality/{inspect,render,diff}, /admin/specialist/status,
    # /admin/explain. Unix-socket-only: registered on internal_app alone,
    # NEVER on the public 8099 app (see casa_core.py's `app` router further
    # down). Skipped when runtime is None (some test/fallback boot paths).
    if runtime is not None:
        from personality_admin_handlers import register_personality_admin_routes

        register_personality_admin_routes(internal_app, runtime=runtime)

    # E-12 (v0.37.0): /internal/channel/* family — POSTed by per-engagement
    # casa-engagement-channel MCP servers proxying outbound traffic to Telegram.
    # Only registered if a TelegramChannel instance is available (production
    # boots always have one; some test paths pass None).
    if telegram_channel is not None:
        from channels.channel_handlers import (
            _make_channel_handlers,
            _make_channel_get_handlers,
        )
        # W1: record each reply() text for the claude_code driver's live
        # topic-stream relay reply de-dup. Resolved lazily — the driver is
        # constructed later (in main), so this closure looks it up at request
        # time via the agent module.
        def _record_engagement_reply(engagement_id: str, text: str) -> None:
            try:
                import agent as _agent_mod
                drv = getattr(_agent_mod, "active_claude_code_driver", None)
                if drv is not None:
                    drv.record_reply_text(engagement_id, text)
            except Exception:  # noqa: BLE001 — de-dup hint is best-effort
                pass

        channel_handlers = _make_channel_handlers(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
            record_reply=_record_engagement_reply,
        )
        for path, handler_fn in channel_handlers.items():
            internal_app.router.add_post(path, handler_fn)
        channel_get_handlers = _make_channel_get_handlers(
            engagement_registry=engagement_registry,
        )
        for path, handler_fn in channel_get_handlers.items():
            internal_app.router.add_get(path, handler_fn)
        logger.info(
            "E-12: registered %d POST + %d GET /internal/channel/* routes "
            "(POST: %s; GET: %s)",
            len(channel_handlers), len(channel_get_handlers),
            sorted(channel_handlers.keys()), sorted(channel_get_handlers.keys()),
        )

    async def _unlink_socket_on_cleanup(_app: web.Application) -> None:
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "start_internal_unix_runner: unlink %s on cleanup failed: %s",
                socket_path, exc,
            )

    internal_app.on_cleanup.append(_unlink_socket_on_cleanup)

    runner = web.AppRunner(internal_app)
    await runner.setup()
    site = web.UnixSite(runner, socket_path)
    await site.start()
    # web.UnixSite doesn't accept a mode= kwarg; chmod after bind.
    try:
        os.chmod(socket_path, 0o600)
    except OSError as exc:
        logger.warning(
            "start_internal_unix_runner: chmod 0600 on %s failed: %s",
            socket_path, exc,
        )
    logger.info("Internal Unix-socket runner listening on %s", socket_path)
    return runner


# ---------------------------------------------------------------------------
# Plan 4b/3.6: public-8099 back-compat fallback handlers
#
# These wrap the new internal_handlers in JSON-RPC envelope code (for
# /mcp/casa-framework) and adapt the body shape (for /hooks/resolve).
# Behavior is byte-identical to v0.13.1 — the wrappers exist so that
# pre-v0.14.0 workspaces (whose .mcp.json points at port 8099) keep
# working through the v0.14.x migration window. svc-casa-mcp on port
# 8100 is the canonical path for new workspaces.
# ---------------------------------------------------------------------------


def _make_public_mcp_fallback_handler(
    *,
    tools: list,
    tool_dispatch: dict,
    engagement_registry,
):
    """Public-8099 /mcp/casa-framework JSON-RPC handler.

    Parses JSON-RPC envelope, dispatches via the same internal handler
    that the Unix socket exposes (in-process call), wraps the result
    back into a JSON-RPC envelope.
    """
    from mcp_envelope import (
        PROTOCOL_VERSION, VERSION,
        _jsonrpc_error, _jsonrpc_ok, _tool_schema,
    )

    # Pre-compute the static tools/list response (snapshot at boot).
    tool_schemas = [_tool_schema(t) for t in tools]

    async def handler(request: web.Request) -> web.Response:
        try:
            msg = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700, "Parse error")

        if not isinstance(msg, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request")

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "notifications/initialized":
            return web.Response(status=202)

        if method == "initialize":
            return _jsonrpc_ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "casa-framework", "version": VERSION},
            })

        if method == "tools/list":
            return _jsonrpc_ok(req_id, {"tools": tool_schemas})

        if method == "ping":
            return _jsonrpc_ok(req_id, {})

        if method == "tools/call":
            # Translate JSON-RPC params + X-Casa-Engagement-Id header into the
            # internal-handler body shape, then dispatch in-process.
            name = params.get("name")
            arguments = params.get("arguments") or {}
            eng_id = request.headers.get("X-Casa-Engagement-Id")
            inner_body = {
                "name": name,
                "arguments": arguments,
                "engagement_id": eng_id,
            }
            result = await _dispatch_internal_tools_call(
                body=inner_body,
                tool_dispatch=tool_dispatch,
                engagement_registry=engagement_registry,
            )
            if "error" in result:
                err = result["error"]
                return _jsonrpc_error(req_id, err["code"], err["message"])
            return _jsonrpc_ok(req_id, result)

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    return handler


def _make_public_hooks_fallback_handler(
    *,
    hook_policies: dict,
    executor_hook_policies: dict | None = None,
    engagement_registry=None,
):
    """Public-8099 /hooks/resolve handler.

    Same body shape as the internal handler ({"policy": ..., "payload": ...}).
    Just re-exports the internal factory under a different name for clarity
    at the call site — behavior is identical. H3 (v0.53.0): forwards the
    per-executor hook policies + engagement registry so parameterised
    callbacks apply on this path too.
    """
    from internal_handlers import _make_internal_hooks_resolve_handler
    return _make_internal_hooks_resolve_handler(
        hook_policies=hook_policies,
        executor_hook_policies=executor_hook_policies,
        engagement_registry=engagement_registry,
    )


def _make_public_mcp_get_405_handler():
    """GET /mcp/casa-framework -> 405 Method Not Allowed (mirrors v0.13.1)."""
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(
            status=405, text="Method Not Allowed\n",
            headers={"Allow": "POST"},
        )
    return handler


async def _dispatch_internal_tools_call(
    *,
    body: dict,
    tool_dispatch: dict,
    engagement_registry,
) -> dict:
    """In-process equivalent of POST /internal/tools/call. Returns the
    bare dict the internal handler would have returned in its response
    body — used by the public-8099 JSON-RPC fallback to avoid an HTTP
    round-trip to ourselves.

    Kept separate from _make_internal_tools_call_handler so the public
    fallback doesn't need to synthesize an aiohttp web.Request.
    """
    name = body.get("name")
    arguments = body.get("arguments") or {}
    eng_id = body.get("engagement_id")

    if not isinstance(name, str):
        return {"error": {"code": -32602, "message": "missing name"}}

    fn = tool_dispatch.get(name)
    if fn is None:
        return {"error": {"code": -32602,
                          "message": f"Unknown tool: {name}"}}

    # v0.74.2: terminal binding for the emit_completion idempotency path —
    # mirrors internal_handlers (see _TERMINAL_BINDING_TOOLS there).
    from internal_handlers import _TERMINAL_BINDING_TOOLS
    engagement = None
    if eng_id:
        try:
            rec = engagement_registry.get(eng_id)
        except Exception:  # noqa: BLE001
            rec = None
        if rec is not None and (
                getattr(rec, "status", None) == "active"
                or name in _TERMINAL_BINDING_TOOLS):
            engagement = rec

    from tools import engagement_var
    token = engagement_var.set(engagement)
    try:
        result = await fn(arguments)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "public /mcp/casa-framework fallback: tool %r raised: %s",
            name, exc,
        )
        return {"error": {"code": -32001,
                          "message": f"Tool {name!r} raised: {exc}"}}
    finally:
        engagement_var.reset(token)

    return result


# ------------------------------------------------------------------
# Health endpoint
# ------------------------------------------------------------------


async def healthz(_request: web.Request) -> web.Response:
    """Return a simple health-check response."""
    return web.json_response({"status": "ok"})


# ------------------------------------------------------------------
# Plan 4a Phase E: boot replay for claude_code engagements
# ------------------------------------------------------------------


async def replay_undergoing_engagements(
    *, registry, driver, executor_registry=None,
    engagements_root: str = "/data/engagements",
    telegram_ready=None,
) -> None:
    """On Casa boot: reconstruct s6 services for UNDERGOING claude_code engagements.

    Heal path (Plan 4a.1): when a UNDERGOING engagement's service dir is
    missing but the workspace dir at /data/engagements/<id>/ still exists
    and the executor is registered, re-render the run script and re-plant
    the s6 service dir. Missing workspace dir remains a warn-and-skip case
    (§7.3 of the 4a.1 spec).
    """
    from drivers import s6_rc
    from drivers.workspace import (
        refresh_claude_md, render_log_run_script, render_run_script,
    )

    undergoing = [
        r for r in registry.active_and_idle()
        if r.driver == "claude_code"
    ]
    keep_ids = {r.id for r in undergoing}
    # §3.8/Sol F5: engagements whose recorded plugin artifacts are gone are
    # refused resume — and MUST be excluded from the start_service and
    # background-task loops below, not merely skipped during rendering.
    refused_ids: set[str] = set()

    # v0.83.0 (§A3(b), Sol r6-3/r7-3/4): the BOOT open-question reconciliation
    # owner. Take a PRE-SERVICE snapshot of every claude_code record that has
    # outstanding raw open_questions AND a topic — REGARDLESS of terminal status
    # (the ownership predicate; the summary-adoption-failure path mark_error's the
    # record TERMINAL before refused_ids is even consulted, so a non-terminal
    # filter would miss exactly the case that must still settle). Snapshotting
    # HERE, before any service start / background-task spawn, preserves the
    # invariant that a fresh same-process ask registered by a just-resumed CLI is
    # never captured + expired. A shared claimed-set guarantees exactly one
    # reconciler per record per boot (the attached driver pass OR the casa_core
    # pass below — never both).
    reconcile_snapshots: dict[str, list[dict]] = {}
    reconcile_claimed: set[str] = set()
    _seen_snapshot_ids: set[str] = set()
    for _rec in (list(registry.active_and_idle())
                 + list(registry.terminal_records())):
        if _rec.id in _seen_snapshot_ids:
            continue
        _seen_snapshot_ids.add(_rec.id)
        if getattr(_rec, "driver", None) != "claude_code":
            continue
        if getattr(_rec, "topic_id", None) is None:
            continue
        # B3: record the per-record snapshot ALWAYS — an EMPTY list stays [] (a
        # replay context that reconciles NOTHING), distinct from a missing entry.
        # A record whose open_questions is empty at snapshot time must reconcile
        # nothing so a fresh same-process ask created BETWEEN this snapshot and
        # attach is never fresh-read + expired as prior-process.
        _oq = list(getattr(_rec, "open_questions", ()) or ())
        reconcile_snapshots[_rec.id] = [dict(q) for q in _oq]

    async def _refuse_brief_resume(
        rec, reason: str, *, kind: str = "refuse_teardown_failed",
    ) -> None:
        """Fail-closed teardown of an engagement we refuse to resume
        (r11-B1/r13-B1/r14-B1; B2 Sol r2 reuses it for the migration path via
        ``kind``). Source removal + recompile alone is NOT reliable —
        ``remove_service_dir`` swallows OSError and the later compile can
        fail-and-continue — so run the CHECKED teardown ladder, and land a
        TERMINAL ``kind`` mark when physical containment can't be confirmed
        (the marking ACCOMPANIES the removal, it does not replace it).
        ``registry`` is the real parameter name here; ``_engagement_registry``
        does not exist in this function and would NameError straight into the
        per-record warn-and-continue."""
        logger.warning(
            "boot replay: engagement %s refuses resume — %s; "
            "tearing down", rec.id[:8], reason,
        )
        refused_ids.add(rec.id)
        down = await s6_rc.ensure_service_down(engagement_id=rec.id)
        if down is False:
            try:
                await registry.mark_error(
                    rec.id, kind=kind,
                    message=(
                        f"resume refused ({reason}) but the engagement "
                        "service could not be confirmed down"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort terminal mark
                logger.warning(
                    "boot replay: mark_error(%s) failed for %s: %s",
                    kind, rec.id[:8], exc,
                )
        s6_rc.remove_service_dir(
            svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT, engagement_id=rec.id,
        )

    # §A3(b) boot reconciliation owner — TERMINAL records (Sol r7-3): terminal
    # records are DISJOINT from ``undergoing`` (they never attach), so schedule
    # their readiness-gated reconcile HERE, BEFORE the compile lock — the lock's
    # fast-path return (no undergoing + no orphans) would otherwise skip the tail
    # of this function and a terminal summary-adoption-failure record with a live
    # question would never settle. Claimed so the refused-undergoing pass below
    # never double-settles.
    _schedule_reconcile = getattr(driver, "schedule_boot_reconcile", None)
    if _schedule_reconcile is not None:
        for _trec in registry.terminal_records():
            _tsnap = reconcile_snapshots.get(_trec.id)
            if not _tsnap or _trec.id in reconcile_claimed:
                continue
            try:
                _schedule_reconcile(
                    _trec, _tsnap, telegram_ready, claimed=reconcile_claimed)
            except Exception as exc:  # noqa: BLE001 — best-effort per record
                logger.warning(
                    "boot replay: terminal open-question reconcile for %s "
                    "failed to schedule: %s", _trec.id[:8], exc,
                )

    async with s6_rc._compile_lock:
        # 1. Orphan sweep — dirs for non-UNDERGOING engagements, remove them.
        removed_orphans = s6_rc.sweep_orphan_service_dirs(
            svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
            keep_engagement_ids=keep_ids,
        )
        # Also reap stale /tmp/s6-casa-db-* dirs left by the previous
        # container run (L12 leak guard) — /run is fresh tmpfs after a
        # restart, so any prior compiled db still in /tmp is orphaned.
        s6_rc.sweep_orphan_compiled_dbs()

        # Fast path: no UNDERGOING engagements and no orphans were swept →
        # the engagement sources dir is empty and unchanged. Running
        # s6-rc-compile against an empty source dir prints
        # "source /data/casa-s6-services is empty" to stderr at every boot,
        # plus burns one compile + one s6-rc-update for nothing. Skip it.
        if not undergoing and not removed_orphans:
            return

        # 2. Heal missing/incomplete service pairs for UNDERGOING
        # engagements. v0.64.0: 'main dir present' no longer means 'unit
        # present' — the predicate is pair-completeness, so torn halves AND
        # legacy (≤v0.63.x, nested-log/) dirs are re-planted, migrating
        # in-flight engagements to the working log pipeline. Each record
        # heals independently: one failure must not abort the others'
        # compile/start below.
        for rec in undergoing:
            try:
                # W3 (r8-B5/r9-B5): re-render the workspace CLAUDE.md from the
                # VERBATIM origin["brief"] for EVERY resumed brief-bearing
                # engagement — placed at the TOP of the loop, BEFORE the
                # service_pair_complete fast path. /data/casa-s6-services
                # persists across restarts, so an ordinary restart takes the
                # early `continue`; a refresh after the pair-rewrite would never
                # run. Resolve the executor via definition_any (r10-B5) so a
                # specialist DISABLED after launch still resumes; registry
                # absent OR unresolved → fail-closed refuse (checked teardown).
                brief_defn = None
                has_brief = bool(rec.origin.get("brief"))
                if has_brief:
                    brief_defn = (
                        executor_registry.definition_any(rec.role_or_type)
                        if executor_registry is not None else None
                    )
                    if brief_defn is None:
                        await _refuse_brief_resume(
                            rec,
                            "no executor_registry passed"
                            if executor_registry is None
                            else f"executor type {rec.role_or_type!r} "
                                 "not resolvable (definition_any → None)",
                        )
                        continue
                    ws_dir = os.path.join(engagements_root, rec.id)
                    try:
                        refresh_claude_md(ws_dir, defn=brief_defn, rec=rec)
                    except Exception as exc:  # noqa: BLE001 — fail-closed
                        await _refuse_brief_resume(
                            rec, f"CLAUDE.md refresh failed: {exc}",
                        )
                        continue

                if s6_rc.service_pair_complete(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=rec.id,
                ):
                    # B1 (Sol r1): a COMPLETE pre-v0.75 pair still carries an
                    # old run script that emits neither the stream-json output
                    # nor the ``casa_control`` spawn NDJSON frame, so the new
                    # _InboundSpool never arms and every resumed operator turn
                    # queues forever. Detect the stale script and DROP the pair
                    # so the heal path below re-renders it from the current
                    # template (reusing the existing incomplete-pair heal — no
                    # duplication). A current pair keeps the fast-path continue.
                    if not s6_rc.run_script_is_stale(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=rec.id,
                    ):
                        continue
                    logger.info(
                        "boot replay: migrating pre-v0.75 run script for "
                        "engagement %s (%s) — re-rendering pair",
                        rec.id[:8], rec.role_or_type,
                    )
                    s6_rc.remove_service_dir(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=rec.id,
                    )
                    # B2 (Sol r2): remove_service_dir SWALLOWS rmtree failures,
                    # so a surviving old main (full or partial removal) would
                    # collide with write_service_dir's exist_ok=False re-plant
                    # and leave a stale, unlogged main whose spawn frames never
                    # reach the relay. VERIFY the pair is actually gone; if not,
                    # fail CLOSED (checked teardown + terminal mark) rather than
                    # compiling/starting a stale pair.
                    if not s6_rc.service_dirs_absent(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=rec.id,
                    ):
                        await _refuse_brief_resume(
                            rec,
                            "stale pre-v0.75 pair removal did not complete "
                            "(service dir survivor)",
                            kind="refuse_migration_failed",
                        )
                        continue

                # M7: never plant a service for an engagement whose workspace
                # is gone. The generated run script does `set -e;
                # cd <workspace>`, so a missing workspace makes the longrun
                # exit immediately and s6 respawns it forever. Warn-and-skip
                # (4a.1 §7.3) instead.
                ws_dir = os.path.join(engagements_root, rec.id)
                if not os.path.isdir(ws_dir):
                    logger.warning(
                        "boot replay: workspace dir %s missing for engagement "
                        "%s — leaving UNDERGOING (warn-and-skip, 4a.1 §7.3)",
                        ws_dir, rec.id[:8],
                    )
                    continue

                if executor_registry is None:
                    logger.warning(
                        "boot replay: service pair missing for engagement %s "
                        "— no executor_registry passed; leaving UNDERGOING",
                        rec.id[:8],
                    )
                    continue

                # r11-B2: for brief-bearing records reuse the already-resolved
                # definition_any result (which resolves DISABLED specialists),
                # so a disabled-definition engagement with an INCOMPLETE pair
                # heals instead of silently not healing. Brief-LESS records keep
                # today's get() behaviour EXACTLY (None for disabled → refuse).
                defn = brief_defn if has_brief else executor_registry.get(
                    rec.role_or_type)
                if defn is None:
                    logger.warning(
                        "boot replay: cannot heal engagement %s — executor "
                        "type %r not registered; leaving UNDERGOING",
                        rec.id[:8], rec.role_or_type,
                    )
                    continue

                # §3.8: replay renders --plugin-dir flags from the engagement's
                # RECORDED artifacts, never a re-resolution of current
                # assignments. A missing recorded artifact refuses resume
                # (fail-closed) — start/background loops skip it via refused_ids.
                missing = [pa for pa in rec.plugin_artifacts
                           if not os.path.isdir(pa.get("path", ""))]
                if missing:
                    names = ", ".join(pa.get("name", "?") for pa in missing)
                    logger.warning(
                        "boot replay: engagement %s refuses resume — plugin "
                        "artifact(s) missing: %s", rec.id[:8], names)
                    if rec.topic_id is not None:
                        try:
                            await driver._send_to_topic(
                                rec.topic_id,
                                "⚠️ This engagement can't resume: its pinned "
                                f"plugin artifact(s) are missing ({names}). "
                                "Start a new engagement.")
                        except Exception:  # noqa: BLE001 — best-effort notice
                            pass
                    refused_ids.add(rec.id)
                    continue

                # Clear stale/legacy/torn dirs first — write_service_dir
                # mkdirs with exist_ok=False (a surviving -log sibling would
                # otherwise collide).
                s6_rc.remove_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=rec.id,
                )

                # Re-render run + log scripts.
                run_script = render_run_script(
                    engagement_id=rec.id,
                    permission_mode=defn.permission_mode or "acceptEdits",
                    extra_dirs=list(defn.extra_dirs or []),
                    plugin_dirs=[pa["path"] for pa in rec.plugin_artifacts],
                )
                log_script = render_log_run_script(engagement_id=rec.id)
                s6_rc.write_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=rec.id,
                    run_script=run_script,
                    depends_on=["init-setup-configs"],
                    log_run_script=log_script,
                )
                # Ensure FIFO exists — it might have been wiped alongside the
                # svc dir.
                fifo = os.path.join(engagements_root, rec.id, "stdin.fifo")
                try:
                    if (os.path.isdir(os.path.dirname(fifo))
                            and not os.path.exists(fifo)):
                        os.mkfifo(fifo, 0o600)
                except OSError as exc:
                    logger.warning(
                        "boot replay: mkfifo %s failed: %s — continuing",
                        fifo, exc,
                    )
                logger.info(
                    "boot replay: healed engagement %s (%s)",
                    rec.id[:8], rec.role_or_type,
                )
            except Exception as exc:  # noqa: BLE001 — per-record isolation
                logger.warning(
                    "boot replay: heal failed for engagement %s: %s — "
                    "continuing (compile-path prune keeps sources sane)",
                    rec.id[:8], exc,
                )

        # 3. Single compile + update pass.
        await s6_rc._compile_and_update_locked()

        # 4. v0.79.0 (§5/F7): adopt the pinned summary BEFORE starting each
        # service, then start. §5 forbids a running engagement without a
        # summary, so an adoption failure ABORTS that engagement (mark error +
        # skip start) rather than starting it summary-less — fail-closed, not
        # the old fail-open "log and continue after start". A fresh v0.79 record
        # (summary already persisted) or a topic-less one adopts as a no-op.
        adopt = getattr(driver, "adopt_summary_if_missing", None)
        for rec in undergoing:
            if rec.id in refused_ids:        # Sol F5: no service was written
                continue
            if adopt is not None:
                try:
                    await adopt(rec)
                except Exception as exc:  # noqa: BLE001 — §5 abort rule
                    logger.warning(
                        "boot replay: summary adopt-on-attach failed for %s: "
                        "%s — aborting resume (not starting summary-less)",
                        rec.id[:8], exc,
                    )
                    refused_ids.add(rec.id)
                    try:
                        await registry.mark_error(
                            rec.id, kind="summary_adopt_failed",
                            message=(
                                "resume aborted: pinned-summary adoption failed "
                                f"({exc})"
                            ),
                        )
                    except Exception:  # noqa: BLE001 — best-effort terminal mark
                        logger.warning(
                            "boot replay: mark_error(summary_adopt_failed) "
                            "failed for %s", rec.id[:8], exc_info=True,
                        )
                    try:
                        await s6_rc.ensure_service_down(engagement_id=rec.id)
                    except Exception:  # noqa: BLE001 — best-effort teardown
                        logger.warning(
                            "boot replay: ensure_service_down after adopt "
                            "failure for %s failed", rec.id[:8], exc_info=True,
                        )
                    continue
            try:
                await s6_rc.start_service(engagement_id=rec.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "boot replay: start_service(%s) failed: %s",
                    rec.id[:8], exc,
                )

    # 5. Background tasks OUTSIDE the lock (long-lived).
    for rec in undergoing:
        if rec.id in refused_ids:            # Sol F5: refused resume / F7 abort
            continue
        # v0.79.0 (§5, F7): the pinned-summary adopt-on-attach now runs in the
        # start loop ABOVE (BEFORE service start, aborting the resume on failure)
        # — a summary-less running engagement is no longer possible. Background
        # tasks build the controller that adopts the (now-guaranteed) summary id.
        try:
            # §A3(b): thread the PRE-SERVICE snapshot + shared claimed-set + the
            # Telegram-readiness event so the attached record's reconcile CLAIMS
            # itself (one reconciler/boot) and runs only after channel readiness.
            driver._spawn_background_tasks(
                rec,
                reconcile_snapshot=reconcile_snapshots.get(rec.id),
                reconcile_claimed=reconcile_claimed,
                telegram_ready=telegram_ready,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "boot replay: background tasks for %s failed: %s",
                rec.id[:8], exc,
            )

    # §A3(b) boot reconciliation owner — REFUSED-undergoing records (Sol r7-3):
    # an undergoing record that REFUSED attachment (missing workspace/artifacts,
    # refused brief resume, summary-adoption failure that mark_error'd it) never
    # got the attached ``_spawn_background_tasks`` reconcile pass and would stay
    # visibly live forever. casa_core owns whatever remains UNCLAIMED (terminal
    # records were already claimed pre-lock; attached records claimed themselves).
    if _schedule_reconcile is not None:
        for _eid, _snap in reconcile_snapshots.items():
            if _eid in reconcile_claimed:
                continue
            _rec = registry.get(_eid)
            if _rec is None:
                continue
            try:
                _schedule_reconcile(
                    _rec, _snap, telegram_ready, claimed=reconcile_claimed)
            except Exception as exc:  # noqa: BLE001 — best-effort per record
                logger.warning(
                    "boot replay: casa_core-owned open-question reconcile for "
                    "%s failed to schedule: %s", _eid[:8], exc,
                )


async def reconcile_terminal_spools(*, registry, driver) -> None:
    """v0.79.0 (§3): terminal boot-reconciliation owner.

    Alongside the active-engagement replay scan, drain the inbound spools of
    TERMINAL engagements that still hold pending receipts/notices — a drain
    that crashed after the terminal commit, or a Telegram send that failed
    before finalize. Each drains to the topic if it still exists, else
    WARN-drops (the topic is gone; nothing to notify into). Pending entries
    therefore retry across restarts until sent or their topic disappears.
    """
    reconcile = getattr(driver, "reconcile_terminal_spool", None)
    if reconcile is None:
        return
    for rec in registry.terminal_records():
        if rec.driver != "claude_code":
            continue
        try:
            await reconcile(rec)
        except Exception as exc:  # noqa: BLE001 — best-effort per record
            logger.warning(
                "terminal spool reconcile failed for %s: %s",
                rec.id[:8], exc,
            )


# ------------------------------------------------------------------
# /hooks/resolve — CC hook_proxy.sh loopback endpoint
# ------------------------------------------------------------------


def _build_cc_hook_policies(hook_policies: dict) -> dict:
    """Build {policy_name: (matcher_regex, async_callback)} from HOOK_POLICIES.

    Plan 4a.1: policies are now two-tier {matcher, factory}; we invoke each
    factory with no kwargs (the HTTP path does not accept per-call params —
    parameterization lives in the executor's hooks.yaml, which the SDK path
    also consumes verbatim). Unknown-param executor hooks.yaml entries still
    surface through the SDK-path's resolve_hooks validation at executor load
    time; the HTTP path inherits whatever configuration the factory
    with-defaults produces.

    These are the DEFAULT-configured callbacks and serve as the fallback for
    the HTTP path. H3 (v0.53.0): per-executor ``hooks.yaml`` parameters (e.g.
    plugin-developer's ``path_scope`` writable/readable prefixes) are wired in
    separately by :func:`_build_executor_cc_hook_policies` and preferred at
    request time by the /internal/hooks/resolve handler when the engagement
    resolves from the payload cwd; this dict is only the fallback when no
    executor-specific callback applies.
    """
    cc_policies: dict = {}
    for name, entry in hook_policies.items():
        matcher = entry["matcher"]
        callback = entry["factory"]()  # default-configured HookCallback
        cc_policies[name] = (matcher, callback)
    return cc_policies


def _build_executor_cc_hook_policies(executor_registry) -> dict:
    """H3 (v0.53.0): ``{executor_type: {policy_name: (matcher, callback)}}``.

    For every ``claude_code`` executor with a ``hooks.yaml``, parse the file
    and build parameterised ``(matcher, callback)`` entries so the HTTP hook
    path (hook_proxy.sh -> /hooks/resolve) enforces the executor's declared
    ``path_scope`` prefixes / ``commit_size_guard`` limit instead of the
    deny-all factory defaults.

    Boot-time snapshot: an operator edit to an executor ``hooks.yaml`` needs an
    add-on restart to affect the HTTP path (same freshness as the boot-built
    default policies). A per-executor parse failure is logged and skipped so
    that executor simply falls back to the default callbacks.
    """
    import yaml
    from hooks import build_policy_callbacks_from_hooks_yaml

    out: dict = {}
    for t in executor_registry.list_types():
        defn = executor_registry.get(t)
        if defn is None or defn.driver != "claude_code" or not defn.hooks_path:
            continue
        try:
            with open(defn.hooks_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            out[t] = build_policy_callbacks_from_hooks_yaml(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executor %r hooks.yaml param build failed: %s — using defaults",
                t, exc,
            )
    return out


def _bus_loop_targets(agents: dict) -> list[str]:
    """H4 (v0.53.0): bus targets that need a ``run_agent_loop`` consumer.

    Residents (``agents`` roles) + the ``telegram`` outbound target + the
    ``observer`` target. ``observer`` was previously missing, so every
    engagement event sent to target='observer' (subprocess_respawn,
    idle_detected, error tool_results) enqueued forever with no consumer —
    events lost, queue leaked. ``dict.fromkeys`` dedupes while preserving
    order (guards a hypothetical user resident literally named "observer").
    """
    return list(dict.fromkeys(list(agents.keys()) + ["telegram", "observer"]))


def _wire_engagement_permission_relay(
    cc_hook_policies: dict,
    *,
    engagement_registry,
    telegram_channel,
) -> dict:
    """Inject engagement_permission_relay into a built cc_hook_policies dict.

    v0.37.2 (C-1): the relay needs live ``engagement_registry`` +
    ``telegram_channel`` + per-engagement ``PERMISSION_QUEUES`` (shared with
    the Telegram callback producer in ``channel_handlers``), so it can't be
    wired via the parameter-free factory pattern used by HOOK_POLICIES.
    Inject it directly into the built ``(matcher, callback)`` dict instead.

    Mutates and returns ``cc_hook_policies`` for caller convenience.
    """
    from hooks import make_engagement_permission_relay
    from channels.channel_handlers import PERMISSION_QUEUES

    cc_hook_policies["engagement_permission_relay"] = (
        r".*",
        make_engagement_permission_relay(
            engagement_registry=engagement_registry,
            telegram_channel=telegram_channel,
            queues=PERMISSION_QUEUES,
        ),
    )
    return cc_hook_policies


def _wire_engagement_buttons_reminder(
    cc_hook_policies: dict,
    *,
    engagement_registry,
) -> dict:
    """Inject engagement_buttons_reminder into a built cc_hook_policies dict.

    R4 (v0.89.0, buttons-always): a PreToolUse(Skill) salience backstop that
    needs the live ``engagement_registry`` (to resolve an ACTIVE engagement
    from the CC payload's cwd), so — like ``engagement_permission_relay`` — it
    can't be built via the parameter-free HOOK_POLICIES factory pattern. The
    matcher is ``Skill`` so only Skill loads reach the callback (the executor's
    generated .claude/settings.json registers the same Skill matcher, and
    ``build_policy_callbacks_from_hooks_yaml`` skips this policy — no factory —
    so the HTTP resolver falls back to this wired default).

    Mutates and returns ``cc_hook_policies`` for caller convenience.
    """
    from hooks import make_engagement_buttons_reminder

    cc_hook_policies["engagement_buttons_reminder"] = (
        r"Skill",
        make_engagement_buttons_reminder(
            engagement_registry=engagement_registry,
        ),
    )
    return cc_hook_policies


async def _drain_broker_before_channel_shutdown(channel_manager: Any) -> None:
    """Graceful-shutdown barrier (r4-B1/B3): resolve every live
    ``verdict_broker`` request as ``cancelled`` and let its keyboard-edit
    finish-hook flush, BEFORE the channels (Telegram bot, etc.) tear down.

    Must run immediately before ``channel_manager.stop_all()`` — a finish
    hook that fires after the channel is stopped can't edit anything.

    Pinned order (r5-B2): cancel the broker records FIRST so a still-draining
    authorization-challenge setup driver can only find a cancelled request
    (never posts a fresh keyboard during shutdown); THEN await the coordinator
    drivers; THEN flush the broker finish hooks; THEN stop the channels.
    """
    from verdict_broker import BROKER
    BROKER.cancel_all(reason="casa_shutdown")
    await CHALLENGES.drain()
    await BROKER.drain_hooks()
    await channel_manager.stop_all()


_STATUS_PAGE = """\
<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Casa Agent</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, system-ui, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 2rem; }}
  .container {{ max-width: 480px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 1.5rem; color: #f8fafc; }}
  h1 span {{ color: #3b82f6; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem;
           margin-bottom: 1rem; }}
  .card h2 {{ font-size: 0.75rem; text-transform: uppercase;
              letter-spacing: 0.05em; color: #64748b; margin-bottom: 0.75rem; }}
  .row {{ display: flex; justify-content: space-between; padding: 0.35rem 0;
          border-bottom: 1px solid #334155; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: #94a3b8; }}
  .value {{ color: #f1f5f9; font-weight: 500; }}
  .value.on {{ color: #4ade80; }}
  .value.off {{ color: #64748b; }}
  .actions {{ display: flex; gap: 0.75rem; margin-top: 1.5rem; }}
  a.btn {{ display: inline-block; padding: 0.6rem 1.2rem; border-radius: 8px;
           text-decoration: none; font-weight: 500; font-size: 0.9rem;
           transition: opacity 0.15s; }}
  a.btn:hover {{ opacity: 0.85; }}
  a.btn.primary {{ background: #3b82f6; color: #fff; }}
  a.btn.disabled {{ background: #334155; color: #64748b;
                    pointer-events: none; cursor: default; }}
  .footer {{ margin-top: 2rem; font-size: 0.75rem; color: #475569; text-align: center; }}
</style>
</head><body>
<div class="container">
  <h1><span>Casa</span> Agent</h1>

  <div class="card">
    <h2>Agents</h2>
    {agent_rows}
  </div>

  <div class="card">
    <h2>Channels</h2>
    {channel_rows}
  </div>

  <div class="card">
    <h2>System</h2>
    {system_rows}
  </div>

  <div class="actions">
    <a class="btn {terminal_class}" href="{ingress_path}/terminal/">Terminal</a>
    <a class="btn primary" href="{ingress_path}/healthz">Health Check</a>
  </div>

  <div class="footer">Casa Agent v{version}</div>
</div>
</body></html>"""


def _row(label: str, value: str, css: str = "") -> str:
    cls = f' class="value {css}"' if css else ' class="value"'
    return f'<div class="row"><span class="label">{label}</span><span{cls}>{value}</span></div>'


# ------------------------------------------------------------------
# Pure helpers (extracted for testability; see tests/test_casa_core_helpers.py)
# ------------------------------------------------------------------


def _env_int_or(name: str, default: int, *, min_value: int = 0,
                max_value: int | None = None,
                env: dict[str, str] | None = None) -> int:
    """Read a non-negative int from env; fall back to *default* on bad input.

    ``min_value``/``max_value`` clamp the parsed value to the same rails the
    HA add-on schema validates (defence in depth — HA schema-validates normal
    config, but a direct env override or a schema drift must not slip past).

    Extracted as a module-level helper so future items that need the same
    shape (spec 5.2 §9.3 has more env vars coming in item I) can reuse
    it. Mirrors retry._env_int but stays on casa_core until a second
    caller appears — then promote to a shared `env.py` module.
    """
    env = env if env is not None else os.environ
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    if value < min_value:
        logger.warning(
            "%s=%d below minimum %d; using %d",
            name, value, min_value, min_value,
        )
        return min_value
    if max_value is not None and value > max_value:
        logger.warning(
            "%s=%d above maximum %d; using %d",
            name, value, max_value, max_value,
        )
        return max_value
    return value


def _env_float_or(name: str, default: float, *, min_value: float = 0.0,
                   env: dict[str, str] | None = None) -> float:
    """Read a non-negative float from env; fall back to *default* on bad
    input. Float counterpart to :func:`_env_int_or` — Task 6 (spec §4.6)
    needs one for ``SPECIALIST_COST_ALERT_THRESHOLD`` (a USD figure)."""
    env = env if env is not None else os.environ
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("Non-finite %s=%r; using default %s", name, raw, default)
        return default
    if value < min_value:
        logger.warning(
            "%s=%s below minimum %s; using %s",
            name, value, min_value, min_value,
        )
        return min_value
    return value


def _maybe_register_n8n(
    mcp_registry: "McpServerRegistry",
    env: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """Register the ``n8n-workflows`` HTTP MCP server if ``N8N_URL`` is set.

    Generic shared infrastructure — any agent (resident or specialist) that
    declares ``n8n-workflows`` in ``mcp_server_names`` can reach it; the
    per-agent ``tools.allowed`` list governs which workflows each agent
    may invoke. Matches the shape of the ``homeassistant`` env-gated
    block in ``main()``.

    Returns the registered server config dict, or ``None`` when
    ``N8N_URL`` is unset or whitespace-only.
    """
    env = env if env is not None else os.environ
    url = (env.get("N8N_URL") or "").strip()
    if not url:
        return None
    api_key = (env.get("N8N_API_KEY") or "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    mcp_registry.register_http(
        name="n8n-workflows",
        url=url,
        headers=headers,
    )
    logger.info("Registered n8n-workflows MCP server")
    return mcp_registry.resolve(["n8n-workflows"]).get("n8n-workflows")


async def wire_tina_ha_facade(
    mcp_registry: "McpServerRegistry",
    facade: Any,
    agents: Mapping[str, Any],
    *,
    tina_role: str = "butler",
) -> None:
    """Publish Tina's eager HA schema and retire her stale SDK clients."""
    mcp_registry.register_role_sdk(
        "homeassistant", tina_role, facade.server_config,
    )
    agent = agents.get(tina_role)
    if agent is not None:
        await agent.invalidate_tool_surface()


async def _start_tina_ha_facade(
    mcp_registry: "McpServerRegistry",
    role_configs: Mapping[str, Any],
    agents: Mapping[str, Any],
    *,
    ha_mcp_url: str,
    supervisor_token: str,
    env: Mapping[str, str] | None = None,
    tina_role: str = "butler",
) -> HomeAssistantFacade | None:
    """Start and publish Tina's eager Home Assistant facade."""
    env = env if env is not None else os.environ
    if env.get("TINA_HA_FACADE_ENABLED", "true").strip().lower() == "false":
        logger.info("ha_facade_disabled")
        return None
    tina_config = role_configs.get(tina_role)
    if (
        not supervisor_token
        or tina_config is None
        or "ha_voice" not in (getattr(tina_config, "channels", ()) or ())
    ):
        return None

    facade: HomeAssistantFacade

    async def _schema_changed() -> None:
        await wire_tina_ha_facade(
            mcp_registry, facade, agents, tina_role=tina_role,
        )

    facade = HomeAssistantFacade(
        ha_mcp_url,
        {"Authorization": f"Bearer {supervisor_token}"},
        on_schema_change=_schema_changed,
    )
    try:
        await facade.start()
    except Exception:  # noqa: BLE001 — raw upstream details may hold secrets
        try:
            await facade.aclose()
        except Exception:  # noqa: BLE001 — degraded boot remains available
            pass
        logger.warning("ha_facade_initialization_failed status=degraded")
        return None
    await wire_tina_ha_facade(
        mcp_registry, facade, agents, tina_role=tina_role,
    )
    return facade


async def _close_tina_ha_facade(
    facade: Any | None,
    *,
    timeout: float = 15.0,
) -> None:
    """Close the optional eager HA facade without wedging Casa shutdown."""
    if facade is None:
        return
    try:
        await asyncio.wait_for(facade.aclose(), timeout=timeout)
    except Exception:  # noqa: BLE001 — shutdown must remain available
        logger.warning("ha_facade_close_failed")


# Max webhook request body (spec A3). Larger requests are rejected before read.
_WEBHOOK_BODY_MAX = 64 * 1024


def _make_webhook_handler(
    *,
    webhook_rate_limiter: Any,
    webhook_secret: str,
    trigger_registry: Any,
    default_role: str,
    bus: Any,
    secrets_dir: str | Path = "/data/webhook_secrets",
):
    """Build the wildcard ``/webhook/{name}`` handler.

    Request pipeline (spec A3): rate limit → bounded body read (64 KiB) →
    name lookup (unknown ⇒ 404) → PER-TRIGGER auth verify (fail ⇒ 401) →
    dispatch a SCHEDULED bus message to the registered role.

    Auth is per-trigger (spec A1): each webhook trigger declares an ``auth``
    policy (``hmac_body`` uses the global ``webhook_secret``; ``static_header``/
    ``timestamped_hmac`` use a per-trigger secret under ``secrets_dir``). An
    empty/absent secret fails closed. 404 precedes auth (names are non-secret,
    r3 design decision) so the policy can be selected by name.

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_webhook_handler.py``.
    """
    import webhook_auth
    secrets_dir = Path(secrets_dir)
    if webhook_secret:
        import log_redact as _lr
        _lr.register_secret(webhook_secret)

    import log_redact

    def _secret_for(name: str, policy: dict) -> bytes:
        mode = policy.get("mode", "hmac_body")
        if mode == "hmac_body":
            return webhook_secret.encode() if webhook_secret else b""
        owner = policy.get("secret_owner", "casa")
        # casa: mint-if-absent so the operator can read + provision it;
        # provider: read-only (imported out of band). Sol shipB-r1 P1-6: a
        # filesystem failure here (unwritable/full secrets dir) must degrade
        # to an EMPTY secret — which never authenticates (401) — not a 500.
        try:
            got = webhook_auth.ensure_secret(
                name, owner=owner, secrets_dir=secrets_dir)
        except Exception:  # noqa: BLE001 — fail closed, never fail open/500
            logger.warning("webhook secret read/mint failed (%s)", name,
                           exc_info=True)
            return b""
        if got:
            # Register for exact-value log redaction (spec A2) so a per-trigger
            # secret can never surface in Casa's application logs.
            try:
                log_redact.register_secret(got.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 — redaction is best-effort
                pass
        return got or b""

    def _verify(request: web.Request, body: bytes, name: str, policy: dict) -> bool:
        return webhook_auth.verify(
            policy.get("mode", "hmac_body"),
            body=body,
            headers=request.headers,
            secret=_secret_for(name, policy),
            header_name=policy.get("header", "X-Webhook-Signature"),
            tolerance_secs=int(policy.get("tolerance_secs", 300)),
            now=int(time.time()),
        )

    async def webhook_handler(request: web.Request) -> web.Response:
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        # Bounded body read (spec A3): reject a declared oversize Content-Length
        # early, AND stream-read with a hard cap so a chunked/Transfer-Encoding
        # request cannot buffer past 64 KiB (Terra ship-review P1).
        if request.content_length is not None and request.content_length > _WEBHOOK_BODY_MAX:
            return web.json_response({"error": "payload too large"}, status=413)
        chunks: list[bytes] = []
        read = 0
        async for chunk in request.content.iter_chunked(8192):
            read += len(chunk)
            if read > _WEBHOOK_BODY_MAX:
                return web.json_response({"error": "payload too large"}, status=413)
            chunks.append(chunk)
        body = b"".join(chunks)

        name = request.match_info.get("name", "")
        target_role = trigger_registry.get_webhook_target(name)
        if target_role is None:
            return web.json_response(
                {"error": "unknown webhook"}, status=404,
            )

        policy = trigger_registry.get_auth_policy(name) or {"mode": "hmac_body"}
        if not _verify(request, body, name, policy):
            return web.json_response(
                {"error": "invalid signature"}, status=401,
            )

        # Parse the ALREADY-READ body (the streaming cap above consumed
        # request.content, so request.json() would re-read empty — Terra
        # ship-review P2). Fall back to raw text for non-JSON payloads.
        try:
            payload = json.loads(body)
        except Exception:
            payload = body.decode("utf-8", errors="replace")

        msg = BusMessage(
            type=MessageType.SCHEDULED,
            source="webhook",
            target=target_role,
            content=f"Webhook '{name}' triggered with payload: {payload}",
            channel="webhook",
            context={
                "webhook_name": name,
                "cid": request.get("cid") or new_cid(),
                # Release A: server-set, unspoofable containment markers. A
                # webhook_trigger turn is UNTRUSTED (third-party content) → the
                # restricted runtime + public-floored recall clearance. Fresh
                # UUID chat_id makes each dispatch a one-shot that can never
                # resume another session.
                "_origin_route": "webhook_trigger",
                "_origin_clearance": trigger_registry.get_clearance(name),
                "chat_id": str(uuid.uuid4()),
            },
        )
        await bus.send(msg)
        return web.json_response({"status": "accepted"})

    return webhook_handler


def _make_telegram_update_handler(*, get_telegram_channel, webhook_secret: str):
    """Build the ``POST /telegram/update`` webhook handler.

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_telegram_update_handler.py``.

    L4: the ``X-Telegram-Bot-Api-Secret-Token`` header is compared with
    ``hmac.compare_digest`` (constant-time) rather than ``!=`` to avoid a
    timing side-channel on the shared webhook secret. BOTH sides are
    encoded to bytes: ``compare_digest`` raises ``TypeError`` on non-ASCII
    ``str`` inputs, and the header value is attacker-controlled (and a
    user-supplied ``webhook_secret`` may be non-ASCII), so a non-ASCII
    header must yield 403, not a 500.
    """

    async def telegram_update_handler(request: web.Request) -> web.Response:
        telegram_channel = get_telegram_channel()
        if telegram_channel is None:
            return web.json_response({"error": "telegram not configured"}, status=404)
        if webhook_secret:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(
                token.encode("utf-8"), webhook_secret.encode("utf-8"),
            ):
                return web.Response(status=403)
        payload = await request.json()
        await telegram_channel.process_webhook_update(payload)
        return web.Response(status=200)

    return telegram_update_handler


def _make_invoke_handler(
    *,
    webhook_rate_limiter: Any,
    webhook_secret: str,
    bus: Any,
    assistant_role: str,
    role_configs: Mapping[str, Any],
):
    """Build the ``POST /invoke/{agent}`` direct-invocation handler.

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_invoke_handler_body_validation.py``. L3: a body that
    parses to a non-dict (``[1]``, ``"hi"``, ``42``, ``null``) is
    rejected with the same 400 the handler already uses for malformed
    JSON, and an explicit ``"context": null`` is normalized to ``{}``
    instead of raising ``TypeError`` at item-assignment.

    Fail-closed channel-capability gate (spec A3): only a resident that
    declares ``webhook`` in its ``channels:`` list is invoke-reachable.
    """

    def _verify(request: web.Request, body: bytes) -> bool:
        if not webhook_secret:
            return True
        sig = request.headers.get("X-Webhook-Signature", "")
        expected = hmac.new(
            webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    async def invoke_handler(request: web.Request) -> web.Response:
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        # Release A (spec A1): /invoke is fail-closed. With no global secret
        # (webhook auth disabled) the route is effectively OFF — it must never
        # accept an unauthenticated arbitrary-prompt request. Returns 403 rather
        # than 401 to signal the route is disabled, not merely mis-signed.
        if not webhook_secret:
            return web.json_response(
                {"error": "webhook auth disabled"}, status=403)

        body = await request.read()
        if not _verify(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)

        agent_role = request.match_info.get("agent", assistant_role)
        cfg = role_configs.get(agent_role)
        if cfg is None or not agent_allowed_on("webhook", cfg):
            return web.json_response({"error": "unknown agent"}, status=404)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid JSON body"}, status=400)

        prompt = payload.get("prompt", "")
        if not prompt:
            return web.json_response({"error": "missing 'prompt' field"}, status=400)

        context = payload.get("context")
        if not isinstance(context, dict):
            context = {}
        context["cid"] = request["cid"]
        payload["context"] = context
        msg = build_invoke_message(agent_role, prompt, payload)
        try:
            result = await bus.request(msg, timeout=300)
            return web.json_response({"response": str(result.content)})
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout"}, status=504)

    return invoke_handler


def build_invoke_message(
    agent_role: str,
    prompt: str,
    payload: dict[str, Any],
) -> BusMessage:
    """Build a webhook invoke BusMessage with a guaranteed-unique session key.

    Callers may pass ``context.chat_id`` in the payload to pin the session
    (e.g. to continue a prior conversation). Otherwise a fresh UUID is
    assigned so two concurrent invocations do not collide on
    ``webhook:default``.

    Every invoke also gets a fresh correlation id (spec 5.2 §7.2).
    Caller-supplied ``context.cid`` wins so external systems can thread
    their own trace ids through; missing or empty entries are replaced.

    Sanitize-and-preserve (A:§3.5): the caller-supplied ``context`` is an
    EXTERNAL dict (webhook payload) — it is stripped of Casa-reserved
    provenance keys via ``sanitize_external_context`` before Casa's own
    keys (``chat_id``, ``cid``) are merged in, so a caller can never spoof
    ``execution_role``/``message_type``/``source``/etc. Every other
    caller-supplied key (e.g. a caller's own ``cid`` above) is preserved.
    """
    context = sanitize_external_context(payload.get("context"))
    if not context.get("chat_id"):
        context["chat_id"] = str(uuid.uuid4())
    if not context.get("cid"):
        context["cid"] = new_cid()
    # Release A: stamp the unspoofable origin route AFTER sanitization so a
    # caller cannot forge it. /invoke is operator-signed (HMAC) → the trusted
    # "invoke" route (private clearance, full runtime); distinct from the
    # "webhook_trigger" route stamped by the /webhook/{name} dispatch.
    context["_origin_route"] = "invoke"
    return BusMessage(
        type=MessageType.REQUEST,
        source="webhook",
        target=agent_role,
        content=prompt,
        channel="webhook",
        context=context,
    )


# ------------------------------------------------------------------
# Memory backend selection (spec 2.2b §2)
# ------------------------------------------------------------------

from dataclasses import dataclass as _dataclass


@_dataclass
class _SemanticMemoryChoice:
    """Long-term (SemanticMemory) backend pick: hindsight | noop."""
    backend: str            # hindsight | noop
    base_url: str = ""


def resolve_semantic_memory_choice(env: dict[str, str]) -> _SemanticMemoryChoice:
    """Resolve the SemanticMemory backend. ``MEMORY_BACKEND=hindsight`` requires
    ``HINDSIGHT_API_URL`` (no hardcoded ``hindsight`` host — spec §8.8); anything
    else → noop."""
    backend = env.get("MEMORY_BACKEND", "").strip().lower()
    base_url = env.get("HINDSIGHT_API_URL", "").strip()
    if backend == "hindsight":
        if not base_url:
            raise ValueError(
                "MEMORY_BACKEND=hindsight requires HINDSIGHT_API_URL "
                "(Hindsight is reached via its hassio alias/IP, not 'hindsight')"
            )
        return _SemanticMemoryChoice(backend="hindsight", base_url=base_url)
    if backend and backend != "noop":
        logger.warning("MEMORY_BACKEND=%r unrecognized; using noop", backend)
    return _SemanticMemoryChoice(backend="noop")


def build_semantic_memory(choice: _SemanticMemoryChoice) -> "SemanticMemory":
    from semantic_memory import NoOpSemanticMemory
    if choice.backend == "hindsight":
        from hindsight_memory import HindsightSemanticMemory
        logger.info("Hindsight semantic memory initialized (url=%s)", choice.base_url)
        return HindsightSemanticMemory(base_url=choice.base_url)
    logger.info("Semantic memory: NoOp (long-term disabled)")
    return NoOpSemanticMemory()


# ------------------------------------------------------------------
# Agent loader
# ------------------------------------------------------------------


def _build_role_registry(
    *,
    residents: dict,
    specialists: dict,
) -> dict:
    """Merge resident and specialist role→AgentConfig dicts. Fail on overlap.

    Returns a single dict the renamed delegate_to_agent tool resolves
    against. Roles must be globally unique across both tiers — colliding
    roles are a configuration bug (e.g. someone created
    agents/specialists/butler/ while a butler resident already exists).
    """
    overlap = set(residents) & set(specialists)
    if overlap:
        raise ValueError(
            f"duplicate role(s) across residents and specialists: "
            f"{sorted(overlap)} — each role must be unique"
        )
    merged = {}
    merged.update(residents)
    merged.update(specialists)
    return merged


# ------------------------------------------------------------------
# Config-sync operator notification
# ------------------------------------------------------------------


async def notify_config_sync(
    bus: Any,
    *,
    report_path: str = "/data/config-sync-report.json",
) -> None:
    """If the boot reconciler overwrote any runtime customization, push a
    heads-up directly to the operator over the ``telegram`` outbound bus
    target (``_telegram_outbound`` → operator's default chat), then mark the
    report notified to avoid duplicate alerts on an svc-only restart.

    Delivered via the deterministic ``telegram`` outbound router (not an
    LLM turn): a config-overwrite is a system event the operator must always
    see, so we bypass the assistant's turn — which could stay silent (the
    G-3 ``<silent/>`` doctrine-bleed history) and, with ``channel=""``, would
    resolve to no channel and drop the text entirely (``agent.py:230,296``).
    Non-fatal. Spec: 2026-06-08-config-sync-reconciler-design.md §3.6.
    """
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return

    if report.get("notified"):
        return
    over = bool(report.get("conflicts") or report.get("schema_forced") or report.get("casabak"))
    if not over:
        return

    paths = (
        [c["path"] for c in report.get("conflicts", [])]
        + [c["path"] for c in report.get("schema_forced", [])]
        + list(report.get("casabak", []))
    )
    ver = report.get("image_version", "the latest update")
    listed = ", ".join(paths[:8]) + ("…" if len(paths) > 8 else "")
    content = (
        f"Heads up: applying {ver} overwrote {len(paths)} of your config "
        f"customization(s) so casa would keep booting: {listed}. "
        "Say 'reconcile config' and I'll show what changed (via git history) "
        "and carry any of it back."
    )

    # Route to the "telegram" outbound target if a telegram channel exists.
    if "telegram" in getattr(bus, "queues", {"telegram": None}):
        await bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source="config_sync",
            target="telegram",
            content=content,
            channel="telegram",
            context={"cid": new_cid()},
        ))

    report["notified"] = True
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
    except OSError as exc:
        logger.warning("config_sync notify: could not mark report notified: %s", exc)


async def _boot_reconcile_plugin_triggers(
    *, trigger_registry: Any, role_configs: dict,
) -> None:
    """Release B boot seam: derive + route the plugin-declared webhook
    trigger overlay AFTER resident triggers register and BEFORE the site
    serves (so /webhook/plg-… routes exist from the first request).

    Prompting is deferred (``prompt=False`` — Telegram is not polling yet;
    the operator-consent DM fires on the next lifecycle reconcile instead);
    when the reconcile surfaces trigger issues, the health report — freshly
    written WITHOUT them by the pre-service plugin_boot oneshot — is
    regenerated so the post-boot health DM announces e.g.
    ``trigger_pending_ack``. Never fatal: a reconcile failure boots with an
    empty overlay (fail-closed for ingress), not a dead Casa.
    """
    try:
        import trigger_reconcile
        issues = await trigger_reconcile.reconcile_plugin_triggers(
            trigger_registry=trigger_registry, role_configs=role_configs,
            channel_manager=None, prompt=False)
        if issues:
            from tools import _regenerate_plugin_health
            await asyncio.to_thread(_regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("boot plugin-trigger reconcile failed", exc_info=True)


async def notify_plugin_health(
    bus: Any,
    *,
    path: str = "/data/plugin-health.json",
) -> None:
    """Push an operator DM for NEW plugin-health issues (§3.10), via the
    deterministic ``telegram`` outbound router. Deduped by STRUCTURED
    fingerprints (not free text). Marks notified ONLY after a successful
    enqueue, so a Telegram-down boot/mutation retries next time. Non-fatal.
    """
    import plugin_health
    report = plugin_health.load_report(path)
    if not report:
        return
    fps = plugin_health.new_fingerprints(report)
    if not fps:
        return
    fp_set = set(fps)
    # Sol #17: the body must span issues AND warnings — new_fingerprints now
    # includes warning fingerprints, so filtering only `issues` would announce a
    # count of 0 for a warning-only change (and mark it notified anyway).
    entries = [e for e in (list(report.get("issues", []))
                           + list(report.get("warnings", [])))
               if e.get("fingerprint") in fp_set]
    parts = [f"{e.get('name')} ({e.get('reason_code')})" for e in entries[:5]]
    listed = ", ".join(parts) + (f" +{len(entries) - 5} more"
                                 if len(entries) > 5 else "")
    content = (
        f"⚠️ Plugin health: {len(entries)} plugin item(s) need attention: "
        f"{listed}. See /data/plugin-health.json."
    )
    if "telegram" not in getattr(bus, "queues", {"telegram": None}):
        return  # retry next boot/mutation once telegram is up
    try:
        await bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source="plugin_health",
            target="telegram",
            content=content,
            channel="telegram",
            context={"cid": new_cid()},
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin_health notify: enqueue failed: %s", exc)
        return  # not marked → retried next boot/mutation
    plugin_health.mark_notified(fps, path)


# ------------------------------------------------------------------
# Engagement-topic retention sweep (v0.65.0 [AR-8])
# ------------------------------------------------------------------

# Once-per-boot flag for the "grant me Delete messages" operator nag — the
# notify_config_sync-style dedupe, held in module state (not a report file)
# because the underlying condition (needs_permission) re-trips on every
# 6-hour sweep until the operator grants the right. Consumed only on a
# successful notify — a failed delivery is retried at the next sweep.
_topic_permission_notified = False


async def _sweep_engagement_topics(channel_manager: Any, bus: Any) -> None:
    """Periodic topics pass — runs right after the workspace sweep [AR-8].

    Deletes due terminal-engagement topics recorded in the topic ledger
    through the telegram channel. When telegram is unconfigured (no
    channel, or no engagement supergroup) the pass skips cleanly: entries
    kept, no warning spam. Never raises — sweep_topics handles per-entry
    telegram errors itself but can raise on a broken channel object, and a
    broken pass must not kill the shared scheduler job.
    """
    global _topic_permission_notified  # noqa: PLW0603 — once-per-boot dedupe

    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None or not getattr(channel, "engagement_supergroup_id", None):
        return

    import topic_ledger

    try:
        res = await topic_ledger.sweep_topics(
            channel,
            chat_id=channel.engagement_supergroup_id,
            scope="due",
        )
    except Exception as exc:  # noqa: BLE001 — never kill the scheduler job
        logger.warning("topic sweep failed: %s", exc)
        return

    if res.get("deleted"):
        logger.info(
            "topic sweep: deleted=%s kept=%s dropped_mismatched=%s",
            res.get("deleted"), res.get("kept"), res.get("dropped_mismatched"),
        )

    if res.get("needs_permission") and not _topic_permission_notified:
        content = (
            'Casa needs the "Delete messages" admin right in the engagement '
            "supergroup to clean up finished topics — for now they are only "
            "closed, not deleted. Grant it via the group's admin settings "
            "for the bot (DOCS.md Setup step 6) and I'll retry at the next "
            "sweep."
        )
        # Deterministic operator delivery over the telegram outbound
        # target, exactly like notify_config_sync — never an LLM turn.
        # The flag is consumed only on successful delivery: a failed
        # notify must be retried at the next sweep, not swallowed.
        if "telegram" in getattr(bus, "queues", {"telegram": None}):
            try:
                await bus.notify(BusMessage(
                    type=MessageType.NOTIFICATION,
                    source="topic_sweep",
                    target="telegram",
                    content=content,
                    channel="telegram",
                    context={"cid": new_cid()},
                ))
                _topic_permission_notified = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "topic sweep: permission nag notify failed: %s", exc,
                )


# ------------------------------------------------------------------
# Authorization-grant TTL sweep (A:§3.3) — hourly, beside the engagement
# daily sweep. GrantStore has no private loop of its own (unlike
# session_sweeper.py, which owns one) — this scheduler job is its only
# sweep seam.
# ------------------------------------------------------------------


async def _authz_grant_sweep() -> None:
    """Drop every authorization grant past its TTL.

    A grant that is never consumed (the operator never taps Approve, or
    taps after the tool call was abandoned) would otherwise sit in
    memory forever — GrantStore has no other reaper. Never raises: a
    sweep failure must not kill the shared scheduler job.
    """
    try:
        removed = GRANTS.sweep()
        if removed:
            logger.info("authz grant sweep: dropped %d expired grant(s)", removed)
    except Exception:  # noqa: BLE001 — never kill the scheduler job
        logger.warning("authz grant sweep failed", exc_info=True)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


def _rich_text_enabled_from_env(env: dict) -> bool:
    """Whether Telegram rich-text rendering is on (kill-switch default: on).

    Mirrors the ``telegram_rich_text`` app option exported as ``TELEGRAM_RICH_TEXT``.
    """
    return env.get("TELEGRAM_RICH_TEXT", "true").strip().lower() not in (
        "false", "0", "off", "no",
    )


async def _notify_recovered_delegations(
    recovered_jobs,
    job_registry,
    bus,
    *,
    assistant_role: str,
) -> None:
    """Notify restart-orphaned Telegram jobs, then durably acknowledge."""
    from specialist_registry import DelegationComplete

    for job in recovered_jobs:
        if job.creator_peer != "telegram" or job.failure is None:
            continue
        target_role = job.creating_role or assistant_role
        if target_role not in bus.queues:
            logger.error(
                "Orphan delegation %s targets unknown role %r — retained for retry",
                job.id[:8], target_role,
            )
            continue

        # This compatibility signal carries only the stable failure envelope
        # and origin metadata. No specialist output is reintroduced into the
        # resident's context during restart recovery.
        synthetic = DelegationComplete(
            delegation_id=job.id,
            agent=job.specialist_role,
            status="error",
            kind=job.failure.kind,
            message=job.failure.message,
            origin={
                "role": job.creating_role,
                "channel": job.creator_peer,
                "chat_id": job.scope_id,
                "cid": job.origin_route_id or "-",
                "user_text": job.task,
            },
            elapsed_s=0.0,
        )
        try:
            await bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source=job.specialist_role,
                target=target_role,
                content=synthetic,
                channel=job.creator_peer,
                context={
                    "cid": job.origin_route_id or "-",
                    "chat_id": job.scope_id,
                    "delegation_id": job.id,
                },
            ))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad recovery must not block later jobs
            # Do not log exception text/tracebacks: connector failures can
            # include payload or credential material. The durable pending bit
            # retains enough state for the next boot to retry.
            logger.error(
                "Orphan notification failed: id=%s phase=notify — retained",
                job.id[:8],
            )
            continue

        try:
            await job_registry.ack_orphan_notification(job.id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — notification was at-least-once
            logger.error(
                "Orphan notification failed: id=%s phase=ack — retained",
                job.id[:8],
            )
            continue
        logger.warning(
            "Orphan delegation recovered: id=%s agent=%s — NOTIFICATION posted",
            job.id[:8], job.specialist_role,
        )


async def main() -> None:
    """Async entry point for the Casa add-on."""

    # 1. Logging (correlation ids + secret redaction, spec 5.2 §7).
    _log_level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    _log_level = getattr(logging, _log_level_name, logging.INFO)
    install_logging(level=_log_level)
    # P-4 (v0.68.2): detached SDK control-request tasks racing subprocess
    # teardown die with an unretrieved CLIConnectionError that asyncio GC
    # would log at ERROR on every engagement close.
    from sdk_logging import install_sdk_task_noise_filter
    install_sdk_task_noise_filter(asyncio.get_running_loop())
    logger.info("Casa core starting up")
    voice_delivery_config = load_voice_delivery_config()

    observed_cli = await asyncio.to_thread(verify_effective_cli)
    observed_version = observed_cli.split(maxsplit=1)[0]
    logger.info(
        "Claude CLI verified path=%s expected=%s observed=%s",
        CLAUDE_CLI_PATH,
        CLAUDE_CLI_VERSION,
        observed_version,
    )

    # 1a. §8: universal op:// resolution for password-typed addon options.
    # OP_SERVICE_ACCOUNT_TOKEN is already in env (exported by svc-casa/run from
    # the onepassword_service_account_token addon option). Resolve all
    # password-typed options in-place now, before any consumer reads them.
    from secrets_resolver import resolve as _resolve_secret
    _PASSWORD_ENV_VARS = (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "WEBHOOK_SECRET",
    )
    for _var in _PASSWORD_ENV_VARS:
        _raw = os.environ.get(_var, "")
        if _raw:
            try:
                _resolved = _resolve_secret(_raw)
                if _resolved != _raw:
                    os.environ[_var] = _resolved
            except RuntimeError as _exc:
                logger.warning(
                    "secrets_resolver: %s op:// resolution failed: %s — "
                    "using raw value; credential will likely be rejected",
                    _var, _exc,
                )

    # 1b. §5.5 / §8.3: source plugin-env.conf into process env.
    # Resolved after OP_SERVICE_ACCOUNT_TOKEN is available so that op://
    # references inside plugin-env.conf can be resolved by the same `op` CLI.
    from plugin_env_conf import read_entries as _read_plugin_env
    _plugin_env_entries = _read_plugin_env()
    for _var, _value in _plugin_env_entries.items():
        try:
            os.environ[_var] = _resolve_secret(_value)
        except RuntimeError as _exc:
            logger.warning(
                "plugin-env: %s unresolved: %s — plugin's MCP server will fail to start",
                _var, _exc,
            )
    # M22 (v0.49.0): seed reload's deletion-diff snapshot so the first
    # casa_reload(scope='plugin_env') can DROP keys applied at boot that
    # were later removed from plugin-env.conf. Seeding keys whose op://
    # resolution failed above is safe — reload's removal path uses
    # os.environ.pop(var, None), a no-op for absent vars.
    from reload import note_boot_plugin_env as _note_boot_plugin_env
    _note_boot_plugin_env(set(_plugin_env_entries))

    # 2. Long-term semantic memory (spec §5/§4.2): the only memory path —
    # Hindsight or noop (resolve_semantic_memory_choice).
    semantic_memory = build_semantic_memory(resolve_semantic_memory_choice(dict(os.environ)))

    def _agent_home_dir(role: str) -> str:
        """Resident transcript cwd (encoded-cwd dir for get_session_messages /
        delete_session). Matches agent.py's agent_home WHEN ``config.cwd`` is
        unset — the prod default (all shipped configs have ``cwd: ""``). If a
        resident ever sets a non-empty ``config.cwd``, its transcript lands
        there instead and the reaper/save would look in the wrong dir; keep
        ``config.cwd`` empty for residents. (Formula also duplicated in
        session_sweeper/session_saver/agent.py — consolidate in a cleanup.)

        Task 9: the reaper/sweeper read the stored ``agent`` field, which now
        holds the canonical role_id (``resident:butler``). Route it through
        ``agent_home_for_role_id`` (which returns the SAME bare-slug path
        agent.py writes transcripts to); a legacy short-role entry falls back
        to the bare-slug formula."""
        from agent import agent_home_for_role_id
        try:
            return agent_home_for_role_id(role)
        except ValueError:
            return f"/config/agent-home/{role}"

    # 3. Message bus
    bus = MessageBus()

    # 4. Session registry + TTL sweeper (spec 5.2 §6)
    sessions_path = os.path.join(DATA_DIR, "sessions.json")
    session_registry = SessionRegistry(sessions_path)
    # A2: one-shot boot migration off the v1 {channel}-{scope} key schema —
    # idempotent (already-v2 entries are left alone), so safe to run on
    # every boot. Only persists when something actually changed.
    _migration_stats = session_registry.migrate_to_v2()
    if _migration_stats["migrated"] or _migration_stats["dropped"]:
        logger.info(
            "session_registry v2 migration: migrated=%d dropped=%d",
            _migration_stats["migrated"], _migration_stats["dropped"],
        )
        await session_registry.save()
    session_sweeper = SessionSweeper(
        registry=session_registry,
        session_ttl_days=_env_int_or("SESSION_TTL_DAYS", 30, min_value=1),
        webhook_session_ttl_days=_env_int_or(
            "WEBHOOK_SESSION_TTL_DAYS", 1, min_value=1,
        ),
        directory_for=_agent_home_dir,
    )
    freshness_reaper = FreshnessReaper(
        registry=session_registry,
        semantic_memory=semantic_memory,
        directory_for=_agent_home_dir,
    )

    # 5. MCP server registry
    mcp_registry = McpServerRegistry()

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    ha_mcp_url = os.environ.get(
        "CASA_HA_MCP_URL",
        "http://supervisor/core/api/mcp",
    )
    if supervisor_token:
        mcp_registry.register_http(
            name="homeassistant",
            url=ha_mcp_url,
            headers={"Authorization": f"Bearer {supervisor_token}"},
        )
        logger.info("Registered Home Assistant MCP server (url=%s)", ha_mcp_url)

    _maybe_register_n8n(mcp_registry)

    # 5b. Config git repo — initialise (idempotent) and snapshot any
    # manual edits that landed between boots.
    try:
        init_repo(CONFIG_DIR)
        snapshot_manual_edits(CONFIG_DIR)
    except Exception as exc:
        logger.warning("config_git bootstrap failed: %s", exc)

    # 6. Channel manager
    channel_manager = ChannelManager()

    # 7. Framework tools
    from tools import create_casa_tools, init_tools
    import specialist_registry as specialist_registry_module
    from specialist_registry import InstalledSpecialistIndex, SpecialistRegistry
    from job_registry import JobRegistry

    # One durable owner for both delegated execution and voice-delivery state.
    # Load/migrate and recover before constructing the compatibility facade so
    # no second lifecycle table can observe or publish a divergent state.
    job_registry = JobRegistry(
        os.path.join(DATA_DIR, "jobs.json"),
        os.path.join(DATA_DIR, "delegations.json"),
        result_ttl_seconds=voice_delivery_config.delivery_ttl_s,
    )
    await job_registry.load()
    recovered_jobs = await job_registry.recover_after_restart()

    # Task 13: the NEW installed-specialist data model — a SEPARATE tree
    # (/config/specialists/<slug>/) and a SEPARATE object from the legacy
    # SpecialistRegistry below (bundled /config/agents/specialists/). Wired
    # here, before any channel/bus loop starts, so the module-level accessor
    # (specialist_registry.live_collision_slugs/live_installed_specialist_slugs)
    # is populated for the rest of boot — same ordering guarantee Task 8
    # established for the four compiled-personality registries.
    #
    # Task N1b Step 25 (Round-4 fix, finding #2): moved BEFORE
    # SpecialistRegistry construction/.load() (was after, Task 13 baseline)
    # so this SAME index doubles as the source current_specialist_roles_dir
    # reconciles the roles overlay from below — one InstalledSpecialistIndex
    # object, not two, and boot self-heals any installed specialist's
    # operational files (not just the roles overlay) the identical way every
    # casa_reload call site does (specialist_materialize.py, reload.py).
    installed_specialist_index = InstalledSpecialistIndex(
        os.path.join(CONFIG_DIR, "specialists"),
    )
    installed_specialist_index.load()
    specialist_registry_module.set_active_installed_index(installed_specialist_index)

    # Round 6, F3: current_specialist_roles_dir acquires MATERIALIZE_LOCK (a
    # threading.Lock) for its in-lock index reload + op-file self-heal + overlay
    # rebuild. Boot is one-time single-threaded init, but to keep the
    # no-sync-lock-on-the-event-loop invariant ABSOLUTE (no boot exception), run
    # it in a worker thread — the index reload + in-lock publish (publish=True,
    # round 6 F2) + reconcile all move off the loop in one hop. publish=True lets
    # the in-lock body republish the SAME object set_active_installed_index
    # already tracks above (idempotent; authoritative publish is now in-lock).
    from specialist_materialize import current_specialist_roles_dir
    roles_overlay = await asyncio.to_thread(
        current_specialist_roles_dir,
        installed_index=installed_specialist_index,
        specialists_dir=Path(os.path.join(CONFIG_DIR, "specialists")),
        agents_specialists_dir=Path(os.path.join(CONFIG_DIR, "agents", "specialists")),
        publish=True,
    )

    specialist_registry = SpecialistRegistry(
        os.path.join(CONFIG_DIR, "agents", "specialists"),
        job_registry=job_registry,
    )
    specialist_registry.load(roles_dir=str(roles_overlay))

    from engagement_registry import EngagementRegistry
    engagement_registry = EngagementRegistry(
        tombstone_path=os.path.join(DATA_DIR, "engagements.json"),
        bus=bus,
    )
    await engagement_registry.load()

    from executor_registry import ExecutorRegistry
    executor_registry = ExecutorRegistry(
        os.path.join(CONFIG_DIR, "agents", "executors"),
    )
    executor_registry.load()

    # Scheduler + trigger registry constructed here so the get_schedule
    # tool can see the registry via init_tools. The per-role
    # register_agent loop stays below (needs role_configs).
    app = web.Application(middlewares=[cid_middleware])
    scheduler = AsyncIOScheduler(
        timezone=resolve_tz(),
        job_defaults={
            "misfire_grace_time": 600,   # 10 min — covers short Casa restarts
            "coalesce": True,            # collapse missed fires to one
            "max_instances": 1,          # no overlap of same job
        },
    )
    trigger_registry = TriggerRegistry(scheduler=scheduler, app=app, bus=bus)

    # 8. Load agent configs by role
    from agent import Agent

    agents_dir = os.path.join(CONFIG_DIR, "agents")
    policy_lib = load_policies(
        os.path.join(CONFIG_DIR, "policies", "disclosure.yaml"),
    )
    # Round 6, F1 (loop-safety consistency): load_all_agents reconciles every
    # resident's binding via personality_binding.reconcile_resident_binding, which
    # now acquires MATERIALIZE_LOCK (a threading.Lock) around its stage/commit/
    # discard. Run the boot scan in a worker thread so that acquisition never
    # happens on the event loop — matching the F3 treatment of
    # current_specialist_roles_dir above and the reload paths (which already reach
    # load_agent_from_dir via asyncio.to_thread). Pure sync function; returns the
    # same role_configs dict.
    role_configs = await asyncio.to_thread(
        load_all_agents, agents_dir, policies=policy_lib,
    )

    # Personality Phase A, Task 8: derive the read-only persona/binding
    # registries from the loaded resident configs. Residents are the only
    # source of role_slot/binding/compiled_prompt_bundle in Plan 1;
    # specialists/executors carry a role_slot too (for lookups) but no
    # binding/bundle.
    from types import MappingProxyType as _MappingProxyType
    _role_slots: dict = {}
    _persona_packs: dict = {}
    _bindings: dict = {}
    _compiled_prompt_bundles: dict = {}
    for cfg in role_configs.values():
        if cfg.role_slot is None:
            continue
        _role_slots[cfg.role_id] = cfg.role_slot
        if cfg.persona_pack is not None:
            _persona_packs[f"{cfg.persona_pack.persona_id}@{cfg.persona_pack.version}"] = cfg.persona_pack
        if cfg.binding is not None:
            _bindings[cfg.role_id] = cfg.binding
        if cfg.compiled_prompt_bundle is not None:
            _compiled_prompt_bundles[cfg.role_id] = cfg.compiled_prompt_bundle
    _role_slots = _MappingProxyType(_role_slots)
    _persona_packs = _MappingProxyType(_persona_packs)
    _bindings = _MappingProxyType(_bindings)
    _compiled_prompt_bundles = _MappingProxyType(_compiled_prompt_bundles)

    specialist_configs = specialist_registry.all_configs()
    from agent_registry import AgentRegistry
    agent_registry = AgentRegistry.build(
        residents=role_configs, specialists=specialist_configs,
    )
    # Task C.2: build the CasaRuntime container.
    from runtime import CasaRuntime
    # Task 14: constructed exactly once at boot, preserved verbatim across
    # every reload (reload.py mutates runtime.role_configs/agents in place
    # and never reconstructs CasaRuntime).
    from explanation_store import ExplanationStore
    explanation_store = ExplanationStore(Path(DATA_DIR) / "explanations")
    runtime = CasaRuntime(
        agents={},                            # populated below at line ~984
        role_configs=role_configs,
        specialist_registry=specialist_registry,
        executor_registry=executor_registry,
        engagement_registry=engagement_registry,
        agent_registry=agent_registry,
        trigger_registry=trigger_registry,
        mcp_registry=mcp_registry,
        session_registry=session_registry,
        channel_manager=channel_manager,
        bus=bus,
        engagement_driver=None,               # set after step 10 InCasaDriver build
        claude_code_driver=None,              # set after step 10b ClaudeCodeDriver build
        policy_lib=policy_lib,
        config_dir=CONFIG_DIR,
        agents_dir=agents_dir,
        home_root="/config/agent-home",
        defaults_root="/opt/casa",
        semantic_memory=semantic_memory,
        job_registry=job_registry,
        role_slots=_role_slots,
        persona_packs=_persona_packs,
        bindings=_bindings,
        compiled_prompt_bundles=_compiled_prompt_bundles,
        explanation_store=explanation_store,
    )
    # Task 6 (spec §4.6): specialist concurrency cap + per-role cost
    # telemetry. `specialist_max_concurrency` bounds delegations in flight
    # fleet-wide; the per-scope cap (exactly 1) is hard-coded inside
    # SpecialistLimiter, not an option. `specialist_cost_alert_threshold`
    # is the cumulative per-role USD figure past which every further
    # delegation for that role also logs a WARNING. Both env vars are a
    # placeholder read pending Task 7's real HA-options wiring.
    from specialist_limits import SpecialistLimiter, SpecialistTelemetry
    # Clamp to the add-on schema's [1, 20] rail (defence in depth — see
    # _env_int_or). The per-scope cap (exactly 1) is not configurable.
    specialist_max_concurrency = _env_int_or(
        "SPECIALIST_MAX_CONCURRENCY", 2, min_value=1, max_value=20)
    specialist_cost_alert_threshold = _env_float_or(
        "SPECIALIST_COST_ALERT_THRESHOLD", 5.0, min_value=0.0)
    specialist_limiter = SpecialistLimiter(max_global=specialist_max_concurrency)
    specialist_telemetry = SpecialistTelemetry(
        cost_alert_threshold=specialist_cost_alert_threshold)

    init_tools(
        channel_manager, bus, specialist_registry, mcp_registry,
        agent_role_map=_build_role_registry(
            residents=role_configs, specialists=specialist_configs,
        ),
        agent_registry=agent_registry,
        trigger_registry=trigger_registry,
        engagement_registry=engagement_registry,
        executor_registry=executor_registry,
        runtime=runtime,
        specialist_limiter=specialist_limiter,
        specialist_telemetry=specialist_telemetry,
        voice_job_route_cap=voice_delivery_config.route_cap,
    )
    mcp_registry.register_sdk_factory(
        "casa-framework",
        lambda _role, grants: create_casa_tools(grants),
    )
    logger.info("Registered casa-framework MCP tools")

    # Plugin media outbox (v0.73.0 §3.4): init FDs + boot-reap + register the
    # hourly sweep — all BEFORE channels/HTTP go live (steps 12–13) so send_media
    # is ready the moment a turn can fire. One call, unit-tested with a fake
    # scheduler (plugin_outbox.wire); never blocks boot.
    import plugin_outbox
    await plugin_outbox.wire(
        scheduler, os.environ.get("CASA_PLUGIN_OUTBOX_DIR", "/data/plugin-outbox"))

    # Plan 4b §5.1 — ensure every loaded in_casa resident or specialist
    # agent has an agent-home with default plugins seeded from
    # plugins.yaml. Idempotent — runs every boot. Executors deliberately
    # excluded (different cwd; see agent_home.provision_all_homes
    # docstring).
    from agent_home import provision_all_homes
    provision_all_homes(
        role_configs=role_configs,
        specialist_configs=specialist_configs,
        home_root=Path("/config/agent-home"),
        defaults_root=Path("/opt/casa"),
    )

    if "assistant" not in role_configs:
        raise RuntimeError(
            f"No agent with role 'assistant' found in {agents_dir}. "
            "Casa cannot start without a primary assistant. Check that "
            "agents/assistant/ exists and runtime.yaml declares "
            "`role: assistant`."
        )

    # Unified plugin architecture (§3.9): load the process-local resolver
    # snapshot from disk ONCE before constructing agents (init-plugin-store
    # already imported bundled artifacts + seeded/migrated the registry). Each
    # Agent's _get_plugin_resolution then reads this snapshot.
    import plugin_registry
    await asyncio.to_thread(plugin_registry.reload_snapshot)

    agents: dict[str, Agent] = {}
    loop_tasks: list[asyncio.Task] = []

    for role, cfg in role_configs.items():
        agent = Agent(
            config=cfg,
            semantic_memory=semantic_memory,
            session_registry=session_registry,
            mcp_registry=mcp_registry,
            channel_manager=channel_manager,
            agent_registry=agent_registry,
        )
        bus.register(role, agent.handle_message)
        agents[role] = agent
        logger.info(
            "Agent '%s' registered (name=%s, model=%s, memory=%s)",
            role,
            cfg.character.name,
            cfg.model,
            cfg.memory.read_strategy,
        )

    ha_facade = await _start_tina_ha_facade(
        mcp_registry,
        role_configs,
        agents,
        ha_mcp_url=ha_mcp_url,
        supervisor_token=supervisor_token,
    )
    runtime.agents = agents  # share the dict reference; reload handlers mutate this directly.

    assistant_role = "assistant"

    # 9. Webhook secret (auto-generated if auth enabled, see setup-configs.sh)
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    if not webhook_secret:
        secret_path = os.path.join(DATA_DIR, "webhook_secret")
        if os.path.exists(secret_path):
            with open(secret_path, "r", encoding="utf-8") as fh:
                webhook_secret = fh.read().strip()
    if webhook_secret:
        logger.info("Webhook secret loaded (%d chars)", len(webhook_secret))

    # 9b. Rate limiters (spec 5.2 §8). capacity=0 disables for a channel.
    _telegram_rate_cap = _env_int_or("TELEGRAM_RATE_PER_MIN", 30, min_value=0)
    _voice_rate_cap = _env_int_or("VOICE_RATE_PER_MIN", 20, min_value=0)
    _webhook_rate_cap = _env_int_or("WEBHOOK_RATE_PER_MIN", 60, min_value=0)
    telegram_rate_limiter = RateLimiter(capacity=_telegram_rate_cap, window_s=60.0)
    voice_rate_limiter = RateLimiter(capacity=_voice_rate_cap, window_s=60.0)
    webhook_rate_limiter = RateLimiter(capacity=_webhook_rate_cap, window_s=60.0)
    logger.info(
        "Rate limits: telegram=%s, voice=%s, webhook=%s",
        f"{_telegram_rate_cap}/min" if telegram_rate_limiter.enabled else "off",
        f"{_voice_rate_cap}/min" if voice_rate_limiter.enabled else "off",
        f"{_webhook_rate_cap}/min" if webhook_rate_limiter.enabled else "off",
    )

    # 10. Telegram channel
    public_url = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if public_url in ("null", "None"):
        public_url = ""
    if public_url:
        logger.info("Public URL: %s", public_url)
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_channel = None
    if telegram_token:
        from channels.telegram import TelegramChannel

        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        telegram_delivery = os.environ.get("TELEGRAM_DELIVERY_MODE", "stream")
        telegram_rich_text = _rich_text_enabled_from_env(os.environ)
        telegram_engagement_supergroup_id = int(os.environ.get(
            "TELEGRAM_ENGAGEMENT_SUPERGROUP_ID", "0",
        ) or "0")

        # Derive transport: webhook only possible when public_url is set
        telegram_transport = os.environ.get("TELEGRAM_TRANSPORT", "polling")
        if telegram_transport == "webhook" and not public_url:
            logger.warning(
                "telegram_transport is 'webhook' but public_url is not set; "
                "falling back to polling"
            )
            telegram_transport = "polling"

        webhook_url = public_url if telegram_transport == "webhook" else ""

        telegram_channel = TelegramChannel(
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            default_agent=assistant_role,
            bus=bus,
            webhook_url=webhook_url,
            delivery_mode=telegram_delivery,
            webhook_secret=webhook_secret,
            rate_limiter=telegram_rate_limiter,
            engagement_supergroup_id=telegram_engagement_supergroup_id or None,
            rich_text_enabled=telegram_rich_text,
        )
        channel_manager.register(telegram_channel)
        # NOTE: setup_engagement_features() needs the bot, which is only
        # built once channel_manager.start_all() runs _rebuild(). We defer
        # the call until after start_all() (see step 12 below). v0.18.2
        # fix — was previously called here and silently failed with
        # "'NoneType' object has no attribute 'get_me'", leaving
        # engagement_permission_ok=False forever.
        logger.info(
            "Telegram channel registered (transport=%s, delivery=%s, chat_id=%s)",
            telegram_transport,
            telegram_delivery,
            telegram_chat_id,
        )

    # Register "telegram" as a bus target for outbound routing
    async def _telegram_outbound(msg: BusMessage) -> None:
        ch = channel_manager.get("telegram")
        if ch is not None:
            await ch.send(str(msg.content), msg.context)

    bus.register("telegram", _telegram_outbound)

    # Engagement infrastructure: InCasaDriver + Observer
    from drivers.in_casa_driver import InCasaDriver

    # Phase 3b: in_casa engagements stream via TopicStreamHandle (per-turn
    # edit-in-place, 1s throttle, mirror Ellen's create_on_token pattern
    # in channels/telegram.py:739-859). Bug 1 fix.
    def _topic_stream_factory(topic_id: int):
        assert telegram_channel is not None, (
            "InCasaDriver requires a configured telegram channel"
        )
        return telegram_channel.create_topic_stream(topic_id)

    # Task 6 (spec §4.6): observe interactive specialist ResultMessages so
    # their cost/usage reaches SpecialistTelemetry too (ephemeral sync/async
    # delegations are captured in tools._run_delegated_agent). Only
    # kind="specialist" engagements feed specialist telemetry; executor
    # engagements are out of scope for this counter.
    def _specialist_result_observer(engagement, result_msg) -> None:
        if getattr(engagement, "kind", "") != "specialist":
            return
        from tokens import extract_usage
        specialist_telemetry.record_cost(
            engagement.role_or_type,
            cost_usd=float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0),
            usage=extract_usage(result_msg),
        )

    engagement_driver = InCasaDriver(
        topic_stream_factory=_topic_stream_factory,
        persist_session_id=engagement_registry.persist_session_id,
        result_observer=_specialist_result_observer,
    )

    # claude_code driver: send_to_topic doubles as the live TopicStreamRelay's
    # send_message primitive (W1), so it must RETURN the posted message_id (the
    # relay edits the rolling message by id). Notice/warning callers ignore it.
    async def _send_to_topic(
        thread_id: int, text: str, reply_to_message_id: int | None = None,
    ) -> int | None:
        if telegram_channel is not None:
            # R2a (v0.89.0): route narration/notice topic sends through the RICH
            # primitive so markdown renders as MessageEntity spans (plain text is
            # sent verbatim — render() returns entities=None and falls back).
            if reply_to_message_id is not None:
                # v0.79.0 (§3): reply-quote the operator's message (Sol-verified
                # PTB 22.7 spelling).
                from telegram import ReplyParameters
                return await telegram_channel.send_to_topic_rich(
                    thread_id, text,
                    reply_parameters=ReplyParameters(
                        message_id=reply_to_message_id,
                        allow_sending_without_reply=True,
                    ),
                )
            return await telegram_channel.send_to_topic_rich(thread_id, text)
        return None

    async def _send_to_topic_paged(
        thread_id: int, text: str,
    ) -> int | None:
        # v0.109.0 (G5): paged rich send for ONE-SHOT terminal posts (the
        # completion notice) — a summary over the 4096/100-entity caps ships as
        # several rendered pages instead of raw markdown. Returns the LAST
        # page's message_id (bottom-most — correct high-water anchor).
        if telegram_channel is not None:
            return await telegram_channel.send_response_to_topic(
                thread_id, text)
        return None

    async def _edit_topic_message(
        thread_id: int, message_id: int, text: str, *, clear_keyboard: bool = False,
    ) -> bool:
        if telegram_channel is not None:
            # R2a (v0.89.0): route narration edits through the RICH primitive so
            # EVERY edit re-renders markdown (plain text edits verbatim).
            return await telegram_channel.edit_topic_message_rich(
                thread_id, message_id, text, clear_keyboard=clear_keyboard)
        return False

    async def _delete_topic_message(thread_id: int, message_id: int) -> bool:
        if telegram_channel is not None:
            return await telegram_channel.delete_topic_message(
                thread_id, message_id)
        return False

    async def _send_topic_message_markup(
        thread_id: int, text: str, markup, reply_to: int | None = None,
    ) -> int | None:
        # A9 (v0.83.0): markup-capable discrete send (OutputSequencer.post_discrete).
        if telegram_channel is not None:
            return await telegram_channel.send_topic_message_markup(
                thread_id, text, markup, reply_to=reply_to)
        return None

    async def _edit_topic_message_markup(
        thread_id: int, message_id: int, text, markup,
    ) -> bool:
        # A9 (v0.83.0): markup-capable discrete edit (OutputSequencer.edit_discrete).
        if telegram_channel is not None:
            return await telegram_channel.edit_topic_message_markup(
                thread_id, message_id, text, markup)
        return False

    async def _pin_topic_message(thread_id: int, message_id: int) -> bool:
        # v0.79.0 (§5): best-effort pin of the live summary message.
        if telegram_channel is not None:
            return await telegram_channel.pin_topic_message(thread_id, message_id)
        return False

    # Expose on the agent module so tools.emit_completion / cancel_engagement
    # can find it without circular imports.
    import agent as agent_mod
    agent_mod.active_engagement_driver = engagement_driver
    agent_mod.active_semantic_memory = semantic_memory   # resident long-term (Hindsight seam)
    agent_mod.active_executor_registry = executor_registry
    runtime.engagement_driver = engagement_driver

    # Plan 4a: claude_code driver. Shares send_to_topic with in_casa.
    from drivers.claude_code_driver import ClaudeCodeDriver

    # Plan 4b/3.6: point new workspaces at svc-casa-mcp on port 8100.
    # Pre-v0.14.0 workspaces still hit casa-main's public 8099 (back-compat
    # fallback registered in this same file); see DOCS.md for migration.
    _casa_framework_mcp_url = os.environ.get(
        "CASA_FRAMEWORK_MCP_URL",
        "http://127.0.0.1:8100/mcp/casa-framework",
    )

    claude_code_driver = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        send_to_topic=_send_to_topic,
        casa_framework_mcp_url=_casa_framework_mcp_url,
        # W1: relay edit/delete primitives + registry (advance_interaction_state
        # seam for Task 7's inbound one-turn queue).
        edit_topic_message=_edit_topic_message,
        delete_topic_message=_delete_topic_message,
        # A9 (v0.83.0): markup-capable discrete send/edit for post_discrete /
        # edit_discrete (keyboard-bearing writes through the single writer).
        send_topic_message_markup=_send_topic_message_markup,
        edit_topic_message_markup=_edit_topic_message_markup,
        # v0.109.0 (G5): paged rich sender for terminal completion posts.
        # Explicitly None without a Telegram channel so the sequencer keeps
        # its ordinary _post_notice_locked path (Sol r2: a non-None sender is
        # authoritative — never inject a wrapper that can only return None).
        send_to_topic_paged=(
            _send_to_topic_paged if telegram_channel is not None else None),
        # v0.79.0 (§5): best-effort pin primitive for the live summary.
        pin_topic_message=_pin_topic_message,
        registry=engagement_registry,
        # O-5 (v0.37.9): capture-and-persist SDK session_id so a Casa
        # restart mid-engagement preserves conversation continuity.
        # The driver writes <workspace>/.session_id from its log-tail
        # capture; this hook keeps EngagementRecord.sdk_session_id in
        # lockstep with the on-disk file the run script reads on resume.
        persist_session_id=engagement_registry.persist_session_id,
    )

    # Wire bus sink so subprocess_respawn events reach the observer.
    async def _publish_driver_bus_event(event: dict) -> None:
        await bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source="claude_code_driver",
            target="observer",
            content=event,
            context={"engagement_id": event.get("engagement_id", "-")},
        ))
    claude_code_driver._publish_bus_event = _publish_driver_bus_event
    agent_mod.active_claude_code_driver = claude_code_driver
    runtime.claude_code_driver = claude_code_driver
    # Stash runtime on agent module so reload handlers and tools find it.
    agent_mod.active_runtime = runtime

    # Plan 4a: boot replay for claude_code engagements.
    # v0.83.0 (§A3(b), Sol r9-2/r10-3): the open-question reconcilers are
    # scheduled here (pre-service snapshot) but their EXECUTION is gated on the
    # Telegram channel's readiness event — replay runs long before the channel
    # starts (start_all() below), and an ungated attach-time reconcile would fire
    # its confirmed settle edits against a None bot and fail closed. The channel
    # sets this at its first successful _rebuild; a None channel yields no event
    # (reconciles then run ungated, matching the no-Telegram deploy).
    _telegram_ready = (
        telegram_channel.ready_event if telegram_channel is not None else None)
    try:
        await replay_undergoing_engagements(
            registry=engagement_registry,
            driver=claude_code_driver,
            executor_registry=executor_registry,
            telegram_ready=_telegram_ready,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Plan 4a boot-replay failed — claude_code engagements may be "
            "in an inconsistent state: %s", exc,
        )

    # v0.79.0 (§3): terminal boot-reconciliation — drain terminal engagements
    # whose inbound spool still holds pending receipts/notices.
    try:
        await reconcile_terminal_spools(
            registry=engagement_registry, driver=claude_code_driver,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("terminal spool reconciliation failed: %s", exc)

    from observer import Observer
    observer = Observer(
        bus=bus,
        engagement_registry=engagement_registry,
        model_name=os.environ.get("SECONDARY_AGENT_MODEL", "haiku"),
    )
    await observer.subscribe()
    # L68/L17: stash so _finalize_engagement can prune per-engagement
    # interjection-budget bookkeeping on terminal transition.
    agent_mod.active_observer = observer

    if telegram_channel is not None:
        telegram_channel._engagement_registry = engagement_registry
        telegram_channel._engagement_driver = engagement_driver
        telegram_channel._observer = observer
        telegram_channel._session_registry = session_registry
        telegram_channel._semantic_memory = semantic_memory

        async def _driver_send_user_turn(rec, text, *, tg_message_id=None):
            # §A3 (Sol r10-2): PROPAGATE the enqueue disposition so
            # _deliver_turn_bg can promote (accepted) vs roll back (rejected)
            # the answered reservation.
            if rec.driver == "claude_code":
                return await claude_code_driver.send_user_turn(
                    rec, text, tg_message_id=tg_message_id)
            # in_casa driver has no durable spool / reply-threading (§7
            # follow-up) — drop the id; no reservation disposition.
            await engagement_driver.send_user_turn(rec, text)
            return None
        telegram_channel._driver_send_user_turn = _driver_send_user_turn

        # v0.83.0 (§A3, Sol r7-1): the answered-RESERVATION seam. reserve is
        # SYNCHRONOUS (set in the handler's same section as the high-water
        # advance); rollback is a CAS. Only claude_code engagements have the
        # spool/anchor machinery — in_casa is a no-op.
        def _driver_reserve_answer(rec):
            if rec.driver == "claude_code":
                return claude_code_driver.reserve_answer(rec.id)
            return None
        telegram_channel._driver_reserve_answer = _driver_reserve_answer

        # G4 D2 (v0.96.0): SYNCHRONOUS inbound-ingress reservation — taken at
        # trusted handler entry (under the topic lock, before the background
        # delivery task exists) and released after the spool enqueue resolves.
        # Terminalization refuses while reservations > 0, closing the
        # accepted-but-not-yet-spooled completion race.
        def _driver_reserve_inbound(rec):
            if rec.driver == "claude_code":
                claude_code_driver.reserve_inbound(rec.id)
                return True
            return False
        telegram_channel._driver_reserve_inbound = _driver_reserve_inbound

        def _driver_release_inbound(rec):
            if rec.driver == "claude_code":
                claude_code_driver.release_inbound_reservation(rec.id)
        telegram_channel._driver_release_inbound = _driver_release_inbound

        async def _driver_rollback_answer_reservation(
                rec, token, *, suppress_reanchor=False):
            if rec.driver == "claude_code" and token is not None:
                return await claude_code_driver.rollback_answer_reservation(
                    rec.id, token, suppress_reanchor=suppress_reanchor)
            return False
        telegram_channel._driver_rollback_answer_reservation = (
            _driver_rollback_answer_reservation)

        # v0.79.0 (§3): seal open narration at inbound-handler entry for
        # claude_code engagements (the T1 high-water seam).
        async def _driver_advance_high_water(rec, msg_id):
            if rec.driver == "claude_code":
                await claude_code_driver.advance_topic_high_water_for_inbound(
                    rec.id, msg_id)
        telegram_channel._driver_advance_high_water = _driver_advance_high_water

        # v0.79.0 (§3, F2): route platform-origin topic notices (command
        # replies, resume errors) through the engagement's OUTPUT SEQUENCER so
        # they seal narration + advance the high-water under the single writer.
        # Non-claude_code engagements have no sequencer — post directly.
        async def _driver_post_notice(rec, text):
            if rec.driver == "claude_code":
                await claude_code_driver.post_topic_notice(rec, text)
            else:
                # v0.109.0 (G3): notices carry markdown — render rich.
                await telegram_channel.send_to_topic_rich(rec.topic_id, text)
        telegram_channel._driver_post_notice = _driver_post_notice

        async def _finalize_cancel(rec, reason="user"):
            # F2 (whole-branch r2): PROPAGATE _finalize_engagement's bool so the
            # terminal command path can gate the answered-reservation re-anchor
            # suppression on a successful strict terminal transition.
            from tools import _finalize_engagement
            driver = (claude_code_driver if rec.driver == "claude_code"
                      else engagement_driver)
            return await _finalize_engagement(
                rec, outcome="cancelled", text=f"Cancelled by {reason}.",
                artifacts=[], next_steps=[],
                driver=driver,
            )
        telegram_channel._finalize_cancel = _finalize_cancel

        async def _finalize_complete_user(rec):
            # F2 (whole-branch r2): PROPAGATE the finalize bool (see above).
            from tools import _finalize_engagement
            driver = (claude_code_driver if rec.driver == "claude_code"
                      else engagement_driver)
            return await _finalize_engagement(
                rec, outcome="completed", text="User-marked complete.",
                artifacts=[], next_steps=[],
                driver=driver,
            )
        telegram_channel._finalize_complete_user = _finalize_complete_user

    # 10b. Voice channel
    voice_sse_enabled = os.environ.get(
        "VOICE_SSE_ENABLED", "true",
    ).lower() == "true"
    voice_ws_enabled = os.environ.get(
        "VOICE_WS_ENABLED", "true",
    ).lower() == "true"
    voice_sse_path = os.environ.get("VOICE_SSE_PATH", "/api/converse")
    voice_ws_path = os.environ.get("VOICE_WS_PATH", "/api/converse/ws")
    _default_voice_idle = (
        role_configs["butler"].session.idle_timeout
        if "butler" in role_configs
        else 300
    )
    voice_idle_timeout = int(os.environ.get(
        "VOICE_IDLE_TIMEOUT_SECONDS", str(_default_voice_idle),
    ))

    voice_channel = None
    if voice_sse_enabled or voice_ws_enabled:
        from channels.voice import VoiceChannel
        from channels.voice.channel import VoiceHandoffCoordinator
        from channels.voice.delivery import VoiceDeliveryCoordinator
        from channels.voice.routes import VoiceRouteRegistry

        voice_routes = VoiceRouteRegistry(
            secret_present=bool(webhook_secret),
            agent_configs=role_configs,
            freshness_s=voice_delivery_config.route_freshness_s,
        )
        voice_delivery = VoiceDeliveryCoordinator(job_registry, voice_routes)
        voice_handoff = VoiceHandoffCoordinator(job_registry)
        runtime.voice_route_registry = voice_routes
        runtime.voice_delivery_coordinator = voice_delivery
        runtime.voice_handoff_coordinator = voice_handoff

        voice_channel = VoiceChannel(
            bus=bus,
            default_agent="butler" if "butler" in role_configs else assistant_role,
            webhook_secret=webhook_secret,
            sse_path=voice_sse_path,
            ws_path=voice_ws_path,
            agent_configs=role_configs,
            memory=semantic_memory,
            idle_timeout=voice_idle_timeout,
            sse_enabled=voice_sse_enabled,
            ws_enabled=voice_ws_enabled,
            rate_limiter=voice_rate_limiter,
            route_registry=voice_routes,
            delivery_coordinator=voice_delivery,
            handoff_coordinator=voice_handoff,
        )
        channel_manager.register(voice_channel)
        logger.info(
            "Voice channel registered (sse=%s, ws=%s, idle=%ss)",
            voice_sse_enabled, voice_ws_enabled, voice_idle_timeout,
        )

    # 11. Webhook endpoints

    # N-1 + N-2 (v0.36.0): wildcard /webhook/{name} consults the trigger
    # registry's per-boot allowlist. Unknown names → 404; known names
    # dispatch to the registered role (no longer hardcoded to
    # assistant_role).
    webhook_handler = _make_webhook_handler(
        webhook_rate_limiter=webhook_rate_limiter,
        webhook_secret=webhook_secret,
        trigger_registry=trigger_registry,
        default_role=assistant_role,
        bus=bus,
    )

    invoke_handler = _make_invoke_handler(
        webhook_rate_limiter=webhook_rate_limiter,
        webhook_secret=webhook_secret,
        bus=bus,
        assistant_role=assistant_role,
        role_configs=role_configs,
    )

    # 11. Telegram webhook route (only used when webhook_url is set).
    # L4: constant-time secret-token comparison lives in the extracted
    # factory. The lambda preserves the closure over the local
    # ``telegram_channel`` (assigned earlier in main()).
    telegram_update_handler = _make_telegram_update_handler(
        get_telegram_channel=lambda: telegram_channel,
        webhook_secret=webhook_secret,
    )

    # 12. Status dashboard
    terminal_enabled = os.environ.get("ENABLE_TERMINAL", "false").lower() == "true"
    version = os.environ.get("CASA_VERSION", "dev")

    async def dashboard(request: web.Request) -> web.Response:
        ingress_path = request.headers.get("X-Ingress-Path", "")

        # Agent rows
        agent_rows = ""
        for role, agent in agents.items():
            model = agent.config.model.replace("claude-", "")
            parts = model.split("-")
            if len(parts) >= 3:
                model = f"{parts[0].capitalize()} {parts[1]}.{parts[2]}"
            display = (
                agent.config.character.name
                if agent.config.character.name
                and not agent.config.character.name.startswith("${")
                else role.capitalize()
            )
            agent_rows += _row(display, model)

        # Channel rows
        channel_rows = ""
        if telegram_channel is not None:
            tg_mode = "webhook" if telegram_channel._webhook_url else "polling"
            tg_delivery = telegram_channel._delivery_mode
            channel_rows += _row("Telegram", f"{tg_mode}, {tg_delivery}", "on")
        else:
            channel_rows += _row("Telegram", "not configured", "off")

        if voice_channel is not None:
            transports = []
            if voice_sse_enabled:
                transports.append("SSE")
            if voice_ws_enabled:
                transports.append("WS")
            channel_rows += _row(
                "Voice", ", ".join(transports) or "disabled",
                "on" if transports else "off",
            )
        else:
            channel_rows += _row("Voice", "not configured", "off")

        # System rows
        system_rows = ""
        if public_url:
            system_rows += _row("Public URL", public_url, "on")
        else:
            system_rows += _row("Public URL", "not set", "off")
        try:
            _sem_backend = resolve_semantic_memory_choice(dict(os.environ)).backend
        except Exception:  # noqa: BLE001 — dashboard must never crash on a memory misconfig
            _sem_backend = "noop"
        mem_type = {"hindsight": "Hindsight", "noop": "none"}.get(_sem_backend, _sem_backend)
        system_rows += _row("Memory", mem_type, "on" if _sem_backend != "noop" else "off")
        system_rows += _row("Webhook auth", "enabled" if webhook_secret else "disabled",
                            "on" if webhook_secret else "off")
        total_triggers = sum(len(cfg.triggers) for cfg in role_configs.values())
        system_rows += _row(
            "Triggers", f"{total_triggers} registered",
            "on" if total_triggers else "off",
        )
        system_rows += _row("Terminal", "enabled" if terminal_enabled else "disabled",
                            "on" if terminal_enabled else "off")

        html = _STATUS_PAGE.format(
            agent_rows=agent_rows,
            channel_rows=channel_rows,
            system_rows=system_rows,
            terminal_class="primary" if terminal_enabled else "disabled",
            ingress_path=ingress_path,
            version=version,
        )
        return web.Response(text=html, content_type="text/html")

    # 13. aiohttp app — app was constructed earlier (above init_tools) so
    # trigger_registry could be wired in. Route registrations that reference
    # closures (dashboard, handlers) happen here once those closures exist.
    if voice_channel is not None:
        voice_channel.register_routes(app)
    app.router.add_get("/", dashboard)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/webhook/{name}", webhook_handler)
    app.router.add_post("/invoke/{agent}", invoke_handler)
    app.router.add_post("/telegram/update", telegram_update_handler)
    # Plan 4b/3.6: public-8099 back-compat fallback handlers.
    # New workspaces point at svc-casa-mcp on 127.0.0.1:8100; this block
    # keeps 8099 serving the same routes for pre-v0.14.0 workspaces still
    # pointing here. Removed in v0.14.2 or later (one-release migration).
    from hooks import HOOK_POLICIES as _HOOK_POLICIES
    _cc_hook_policies = _build_cc_hook_policies(_HOOK_POLICIES)
    # v0.37.2 (C-1): engagement_permission_relay needs live deps that the
    # parameter-free factory pattern can't supply; inject via the helper.
    _wire_engagement_permission_relay(
        _cc_hook_policies,
        engagement_registry=engagement_registry,
        telegram_channel=telegram_channel,
    )
    # R4 (v0.89.0): buttons-always PreToolUse(Skill) salience backstop.
    _wire_engagement_buttons_reminder(
        _cc_hook_policies,
        engagement_registry=engagement_registry,
    )
    # H3 (v0.53.0): per-executor hooks.yaml params for the HTTP hook path.
    _executor_cc_policies = _build_executor_cc_hook_policies(executor_registry)
    app.router.add_post(
        "/hooks/resolve",
        _make_public_hooks_fallback_handler(
            hook_policies=_cc_hook_policies,
            executor_hook_policies=_executor_cc_policies,
            engagement_registry=engagement_registry,
        ),
    )

    from tools import CASA_TOOLS
    _public_tool_dispatch = {t.name: t.handler for t in CASA_TOOLS}
    app.router.add_post(
        "/mcp/casa-framework",
        _make_public_mcp_fallback_handler(
            tools=list(CASA_TOOLS),
            tool_dispatch=_public_tool_dispatch,
            engagement_registry=engagement_registry,
        ),
    )
    app.router.add_get(
        "/mcp/casa-framework", _make_public_mcp_get_405_handler(),
    )

    # 13b. Per-agent trigger registration. Registry + scheduler were
    # constructed earlier (needed by init_tools for get_schedule).
    # Register before runner.setup() so webhook routes land in *app*
    # while the router is still mutable.
    for role, cfg in role_configs.items():
        if cfg.triggers:
            trigger_registry.register_agent(
                role=role, triggers=cfg.triggers, channels=cfg.channels,
            )
            logger.info(
                "Registered %d trigger(s) for agent '%s'",
                len(cfg.triggers), role,
            )

    # 13c. Release B: plugin-declared webhook triggers — reconcile the
    # overlay after resident triggers register, before the server starts.
    await _boot_reconcile_plugin_triggers(
        trigger_registry=trigger_registry, role_configs=role_configs,
    )

    # 13d. v0.112.0 (elevenlabs#2): durable post-consent setup episodes —
    # wire the seams (all late-binding: the channel is resolved at call
    # time) and start the supervised worker; pending episodes from a prior
    # boot re-dispatch immediately (crash-safe at-least-once).
    import plugin_setup_episodes as _pse

    def _setup_pending_for(plugin: str) -> int:
        import trigger_reconcile as _tr
        return sum(
            1 for i in _tr.current_issues()
            if getattr(i, "reason_code", "") == "trigger_pending_ack"
            and getattr(i, "name", "") == plugin)

    def _setup_registry_entry(plugin: str) -> dict | None:
        import plugin_grants as _pg
        import plugin_registry as _pr
        import plugin_store as _pstore
        res = _pr.resolve_all()
        rp = next((p for p in res.plugins if p.name == plugin), None)
        if rp is None:
            return None
        snap = _pr.snapshot_registry()
        entry = next(
            (e for e in snap.entries
             if isinstance(e, dict) and e.get("name") == plugin), None)
        try:
            setup = _pstore.manifest_setup_tool(rp.manifest)
        except Exception:  # noqa: BLE001 — malformed manifest ⇒ no hook
            setup = None
        return {
            "artifact_id": rp.artifact_id,
            "targets": list((entry or {}).get("targets") or []),
            "granted_tools": _pg.grants_for_resolved(rp),
            "setup_tool": setup,
        }

    async def _setup_dispatch(role: str, text: str, context: dict) -> bool:
        import trigger_consent as _tc
        ch = channel_manager.get("telegram") if channel_manager else None
        op = _tc.operator_identity(ch) if ch is not None else None
        if op is None:
            return False
        op_chat, op_user = op
        msg = BusMessage(
            type=MessageType.CHANNEL_IN, source="telegram", target=role,
            content=text, channel="telegram",
            context={
                "chat_id": op_chat, "user_id": op_user, "cid": new_cid(),
                **context,  # reserved synthetic/plugin_setup markers —
                            # Casa-composed internal, never external ingress
            },
        )
        return await bus.send_checked(msg) == "accepted"

    async def _setup_notify(text: str) -> None:
        ch = channel_manager.get("telegram") if channel_manager else None
        if ch is None:
            return
        await ch.send_response(text, {})

    _pse.configure(
        dispatch=_setup_dispatch, notify_operator=_setup_notify,
        pending_consents_for=_setup_pending_for,
        resolve_registry_entry=_setup_registry_entry,
    )
    _pse.start_worker()

    runner = web.AppRunner(
        app,
        access_log_class=CasaAccessLogger,
        access_log=logging.getLogger("casa.access"),
    )
    await runner.setup()
    # H1 (defense-in-depth): bind the backend to loopback only. The sole
    # remote consumer is nginx in the SAME container (proxy_pass
    # http://127.0.0.1:8099) and in-container workspace subprocesses reach
    # it via 127.0.0.1; nothing legitimately connects to 8099 over the
    # hassio bridge, so 0.0.0.0 needlessly exposed it to peer containers.
    site = web.TCPSite(runner, "127.0.0.1", 8099)
    await site.start()
    logger.info("HTTP server listening on 127.0.0.1:8099")

    # Plan 4b/3.6: second AppRunner for the Unix-socket internal API
    # consumed by svc-casa-mcp. The same internal handlers are reused
    # in-process by the public-8099 fallback (route registrations above).
    from hooks import HOOK_POLICIES as _HOOK_POLICIES_FOR_INTERNAL
    _internal_hook_policies = _build_cc_hook_policies(_HOOK_POLICIES_FOR_INTERNAL)
    # v0.37.2 (C-1): mirror the public-8099 wiring on the internal-socket
    # path consumed by svc-casa-mcp.
    _wire_engagement_permission_relay(
        _internal_hook_policies,
        engagement_registry=engagement_registry,
        telegram_channel=telegram_channel,
    )
    # R4 (v0.89.0): buttons-always PreToolUse(Skill) salience backstop.
    _wire_engagement_buttons_reminder(
        _internal_hook_policies,
        engagement_registry=engagement_registry,
    )
    from tools import CASA_TOOLS as _CASA_TOOLS_FOR_INTERNAL
    _internal_tool_dispatch = {
        t.name: t.handler for t in _CASA_TOOLS_FOR_INTERNAL
    }
    # E-12 (v0.37.0): pass the live TelegramChannel built at line ~1162 so
    # start_internal_unix_runner can register the /internal/channel/* family.
    # `telegram_channel` is the local variable; None when no TELEGRAM_TOKEN is
    # set (test/fallback boots), in which case the channel routes are skipped.
    internal_runner = await start_internal_unix_runner(
        socket_path="/run/casa/internal.sock",
        tool_dispatch=_internal_tool_dispatch,
        engagement_registry=engagement_registry,
        hook_policies=_internal_hook_policies,
        executor_hook_policies=_executor_cc_policies,
        runtime=runtime,
        telegram_channel=telegram_channel,
    )
    # Track for shutdown.
    runners: list[web.AppRunner] = [runner, internal_runner]

    # 12. Start all channels
    await channel_manager.start_all()

    # 12a. E-F (v0.30.0): engagement-feature setup is now wired into
    # TelegramChannel._rebuild() as a final step after `self._app = app`.
    # Pre-fix, this boot-time call could fire before `_app` was populated
    # if the first `set_webhook` blipped — leaving `engagement_permission_ok`
    # permanently False until manual restart. The new location makes it
    # self-healing on every successful rebuild. Removed redundant call here.

    # 13. Agent loop tasks. H10/H11 (v0.49.0): spawn through the bus so
    # the consumer tasks are tracked — reload reuses the same seam for
    # roles added after boot and cancels tracked tasks on eviction.
    # H4 (v0.53.0): _bus_loop_targets adds "observer" so the observer queue
    # (subscribed above) actually gets drained; observer.subscribe() ran
    # earlier so its queue already exists here.
    for name in _bus_loop_targets(agents):
        if name in bus.queues:
            loop_tasks.append(bus.start_agent_loop(name))

    # 13b. Restart-orphan notifications come from the recovered durable job
    # failures. Voice jobs remain READY for their delivery coordinator; the
    # compatibility notification below is only for the legacy Telegram route.
    await _notify_recovered_delegations(
        recovered_jobs, job_registry, bus, assistant_role=assistant_role,
    )

    # 13c. Surface any default-sync overwrites to the operator (direct
    # telegram outbound — see notify_config_sync).
    await notify_config_sync(bus)
    # init-plugin-store's health report is resolver-only (it ran before
    # agents/executor-registry existed). Now that they are constructed,
    # regenerate with RUNTIME verification (authorization, effective secrets,
    # system requirements, active bindings) so a plugin with missing auth/secret
    # is not green until a mutation. Never block boot on it.
    try:
        from tools import _regenerate_plugin_health
        await asyncio.to_thread(_regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("boot plugin-health runtime regen failed", exc_info=True)
    await notify_plugin_health(bus)

    # 14. Kick off timers.
    # AsyncIOScheduler's AsyncIOExecutor schedules coroutine functions on
    # the running loop directly. A sync lambda that calls create_task
    # gets dispatched to a worker thread instead, where no loop is bound,
    # raising RuntimeError on every fire (silent regression from v0.13.0).
    # Pass the coroutine functions directly with kwargs.
    session_sweeper.start()
    freshness_reaper.start()
    # D-4 (v0.69.0): reap stale engagements FIRST, then run the idle pass —
    # a record past the reap TTL is cancelled outright and must not receive
    # a pointless idle reminder in the same daily run.
    async def _engagement_daily_sweep() -> None:
        # reap resolves the per-record driver itself (claude_code executors
        # need the claude_code driver — v0.69.6); no driver arg here.
        from tools import reap_stale_engagements
        try:
            reaped = await reap_stale_engagements()
            if reaped:
                logger.info("engagement sweep: reaped %d stale engagement(s)", reaped)
        except Exception:  # noqa: BLE001 — reap failure must not starve the idle pass
            logger.warning("engagement reap failed", exc_info=True)
        await engagement_registry.sweep_idle_and_suspend(driver=engagement_driver)

    scheduler.add_job(
        _engagement_daily_sweep,
        trigger="cron",
        id="engagement_idle_sweep",
        hour=8, minute=0,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # A:§3.3 — authorization-grant TTL sweep, hourly. GrantStore has no
    # private loop of its own (unlike session_sweeper.py); this job is
    # its only reap seam.
    scheduler.add_job(
        _authz_grant_sweep,
        trigger="interval",
        id="authz_grant_sweep",
        hours=1,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # Plan 4a.1 §8: workspace sweeper — every 6 hours, removes terminal
    # engagement workspaces past retention. v0.65.0 [AR-8]: the same job
    # then sweeps due terminal-engagement topics off the Telegram sidebar
    # (topic_ledger) — topics and workspaces expire together.
    from drivers.workspace import _sweep_workspaces as _sweep_ws

    async def _sweep_workspaces_and_topics() -> None:
        # Per-side-effect isolation: a workspace-sweep failure must not
        # starve the topics pass (the topics helper itself never raises).
        try:
            await _sweep_ws(engagements_root="/data/engagements")
        except Exception:  # noqa: BLE001
            logger.warning("workspace sweep failed", exc_info=True)
        await _sweep_engagement_topics(channel_manager, bus)

    scheduler.add_job(
        _sweep_workspaces_and_topics,
        trigger="interval",
        id="workspace_sweep",
        hours=6,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start()

    # 15. Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
    except NotImplementedError:
        logger.warning("Signal handlers not supported on this platform")

    # 16. Wait for stop
    logger.info("Casa core running -- waiting for shutdown signal")
    await stop_event.wait()

    # 17. Cleanup
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await session_sweeper.stop()
    await freshness_reaper.stop()
    # NOTE: deliberately do NOT close the plugin outbox here. Its dir-FDs are
    # O_CLOEXEC and process-lived (the OS reclaims them on exit); closing the
    # live singleton during shutdown — before HTTP ingress + agent turns are
    # drained below — would let an in-flight send_media fall through to a
    # dir_fd=None (CWD-relative) op. close() is for test teardown only.

    # AR-9: close every resident/specialist Agent's SDK client pool so no
    # warm subprocess outlives container shutdown. Bounded per-agent so one
    # hung drain can't block the rest of the shutdown sequence.
    for _role, _agent in list(getattr(runtime, "agents", {}).items()):
        aclose = getattr(_agent, "aclose", None)
        if aclose is not None:
            try:
                await asyncio.wait_for(aclose(), timeout=15)
            except Exception:  # noqa: BLE001 — shutdown must complete
                logger.warning("agent %s aclose failed/timed out", _role)
    await _close_tina_ha_facade(ha_facade)

    # H10 (v0.49.0): include consumers spawned after boot by reload
    # (bus.start_agent_loop) — the local loop_tasks list only has the
    # boot-time ones. cancel() is idempotent for already-evicted tasks.
    all_loop_tasks = set(loop_tasks) | set(bus.agent_loop_tasks())
    for task in all_loop_tasks:
        task.cancel()
    await asyncio.gather(*all_loop_tasks, return_exceptions=True)

    # Channel teardown (Telegram bot, voice, etc.) — resolve in-flight broker
    # work first, then stop the channels.
    await _drain_broker_before_channel_shutdown(channel_manager)

    # HTTP ingress teardown: close BOTH AppRunners (public 8099 + the internal
    # unix socket) BEFORE draining the force cleanups below. runner.cleanup()
    # drains in-flight requests then closes the listener, so once it returns no
    # webhook/voice/internal inbound can reach an agent. Moved AHEAD of the
    # force-cleanup drain (and of semantic_memory.close) to make the drain point
    # INGRESS-QUIESCENT — see the F1 rationale below.
    for _r in runners:
        await _r.cleanup()

    # F1 (v0.83.0 whole-branch gate, wave 2): bounded LOOP-drain of the
    # claude_code driver's force-suspend OWNERS (`_force_tasks`) + CANCEL-EXEMPT
    # post-SIGTERM cleanups (`_force_cleanups` — extinction poll + SIGKILL
    # escalation) so a SIGTERM-resistant engagement subprocess is verified
    # extinct rather than orphaned by a premature process exit.
    #
    # Placed AFTER every ingress surface is quiesced — the agent loops are
    # cancelled (above), the channels stopped, and now the HTTP/socket listeners
    # closed — so nothing can spawn a fresh force-suspend or fire an operator-
    # away clear that hands a new cleanup off after the loop-drain settles. The
    # drain itself re-snapshots both surfaces each iteration to catch a handoff
    # that lands mid-drain. Kept BEFORE semantic_memory.close so the memory
    # client outlives any in-flight cleanup. Bounded + truthful — never wedges
    # shutdown (getattr-guarded so a driver without the seam is a no-op).
    _cc_driver = getattr(runtime, "claude_code_driver", None)
    _drain_force = getattr(_cc_driver, "drain_force_cleanups", None)
    if _drain_force is not None:
        try:
            await _drain_force()
        except Exception:  # noqa: BLE001 — shutdown must complete
            logger.warning("force-cleanup drain failed", exc_info=True)

    # No new ingress can bind a job now. Cancel/wait process-local ownership;
    # each task's done callback remains the sole concurrency-permit releaser.
    try:
        await job_registry.close()
    except Exception:  # noqa: BLE001 — shutdown must complete
        logger.warning("job registry close failed", exc_info=True)

    # Close the shared Hindsight client session (L32) so aiohttp does not
    # warn about an unclosed session; no-op for NoOp/other backends.
    try:
        await semantic_memory.close()
    except Exception:  # noqa: BLE001
        logger.warning("semantic memory close failed", exc_info=True)
    logger.info("Casa core shutdown complete")


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
