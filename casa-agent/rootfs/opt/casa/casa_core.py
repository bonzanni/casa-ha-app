"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

# Ensure the Casa package root is on sys.path regardless of cwd
_CASA_ROOT = str(Path(__file__).resolve().parent)
if _CASA_ROOT not in sys.path:
    sys.path.insert(0, _CASA_ROOT)

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent_loader import load_all_agents
from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig
from config_git import init_repo, snapshot_manual_edits
from freshness_reaper import FreshnessReaper
from log_cid import install_logging, new_cid
from casa_core_middleware import cid_middleware, CasaAccessLogger
from mcp_registry import McpServerRegistry
from semantic_memory import SemanticMemory
from policies import load_policies
from session_registry import SessionRegistry
from session_sweeper import SessionSweeper
from rate_limit import RateLimiter, rate_limit_response
from timekeeping import resolve_tz
from trigger_registry import TriggerRegistry

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

    # E-12 (v0.37.0): /internal/channel/* family — POSTed by per-engagement
    # casa-engagement-channel MCP servers proxying outbound traffic to Telegram.
    # Only registered if a TelegramChannel instance is available (production
    # boots always have one; some test paths pass None).
    if telegram_channel is not None:
        from channels.channel_handlers import (
            _make_channel_handlers,
            _make_channel_get_handlers,
        )
        channel_handlers = _make_channel_handlers(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
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

    engagement = None
    if eng_id:
        try:
            rec = engagement_registry.get(eng_id)
        except Exception:  # noqa: BLE001
            rec = None
        if rec is not None and getattr(rec, "status", None) == "active":
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
        render_log_run_script, render_run_script,
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
                if s6_rc.service_pair_complete(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=rec.id,
                ):
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

                defn = executor_registry.get(rec.role_or_type)
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

        # 4. Start each (idempotent under s6-rc change).
        for rec in undergoing:
            if rec.id in refused_ids:        # Sol F5: no service was written
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
        if rec.id in refused_ids:            # Sol F5: refused resume
            continue
        try:
            driver._spawn_background_tasks(rec)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "boot replay: background tasks for %s failed: %s",
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
                env: dict[str, str] | None = None) -> int:
    """Read a non-negative int from env; fall back to *default* on bad input.

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


def _make_webhook_handler(
    *,
    webhook_rate_limiter: Any,
    webhook_secret: str,
    trigger_registry: Any,
    default_role: str,
    bus: Any,
):
    """Build the wildcard ``/webhook/{name}`` handler.

    The handler:

    * applies the global rate limit (shared bucket with /invoke/{agent}),
    * verifies the HMAC-SHA256 signature when ``webhook_secret`` is set
      (so unknown names cannot leak via 404 vs 401 timing),
    * looks up ``name`` in ``trigger_registry.get_webhook_target(name)``
      — returns 404 ``{"error": "unknown webhook"}`` for unknowns
      (N-2, v0.36.0),
    * dispatches a SCHEDULED bus message to the registered role, falling
      back to ``default_role`` only when the registry returns the
      sentinel ``"__assistant_default__"`` (kept for forward parity if
      we want a public open dispatch later — currently unused).

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_webhook_handler.py``.
    """

    def _verify(request: web.Request, body: bytes) -> bool:
        if not webhook_secret:
            return True
        sig = request.headers.get("X-Webhook-Signature", "")
        expected = hmac.new(
            webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    async def webhook_handler(request: web.Request) -> web.Response:
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        body = await request.read()
        if not _verify(request, body):
            return web.json_response(
                {"error": "invalid signature"}, status=401,
            )

        name = request.match_info.get("name", "")
        target_role = trigger_registry.get_webhook_target(name)
        if target_role is None:
            return web.json_response(
                {"error": "unknown webhook"}, status=404,
            )

        try:
            payload = await request.json()
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
):
    """Build the ``POST /invoke/{agent}`` direct-invocation handler.

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_invoke_handler_body_validation.py``. L3: a body that
    parses to a non-dict (``[1]``, ``"hi"``, ``42``, ``null``) is
    rejected with the same 400 the handler already uses for malformed
    JSON, and an explicit ``"context": null`` is normalized to ``{}``
    instead of raising ``TypeError`` at item-assignment.
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

        body = await request.read()
        if not _verify(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)

        agent_role = request.match_info.get("agent", assistant_role)
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
    """
    context = dict(payload.get("context") or {})
    if not context.get("chat_id"):
        context["chat_id"] = str(uuid.uuid4())
    if not context.get("cid"):
        context["cid"] = new_cid()
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
# Main
# ------------------------------------------------------------------


def _rich_text_enabled_from_env(env: dict) -> bool:
    """Whether Telegram rich-text rendering is on (kill-switch default: on).

    Mirrors the ``telegram_rich_text`` app option exported as ``TELEGRAM_RICH_TEXT``.
    """
    return env.get("TELEGRAM_RICH_TEXT", "true").strip().lower() not in (
        "false", "0", "off", "no",
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
        session_sweeper/session_saver/agent.py — consolidate in a cleanup.)"""
        return f"/config/agent-home/{role}"

    # 3. Message bus
    bus = MessageBus()

    # 4. Session registry + TTL sweeper (spec 5.2 §6)
    sessions_path = os.path.join(DATA_DIR, "sessions.json")
    session_registry = SessionRegistry(sessions_path)
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
    if supervisor_token:
        ha_mcp_url = os.environ.get(
            "CASA_HA_MCP_URL",
            "http://supervisor/core/api/mcp",
        )
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
    from specialist_registry import DelegationComplete, SpecialistRegistry

    # Phase 3.1 Task 7: init_tools now takes a SpecialistRegistry so the
    # delegate_to_agent tool can resolve resident + specialist configs.
    # Task 10 replaces this stub with a directory scan + orphan recovery.
    specialist_registry = SpecialistRegistry(
        os.path.join(CONFIG_DIR, "agents", "specialists"),
        tombstone_path=os.path.join(DATA_DIR, "delegations.json"),
    )
    specialist_registry.load()

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
    role_configs = load_all_agents(agents_dir, policies=policy_lib)

    specialist_configs = specialist_registry.all_configs()
    from agent_registry import AgentRegistry
    agent_registry = AgentRegistry.build(
        residents=role_configs, specialists=specialist_configs,
    )
    # Task C.2: build the CasaRuntime container.
    from runtime import CasaRuntime
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
    )
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
    )
    casa_tools_config = create_casa_tools()
    mcp_registry.register_sdk("casa-framework", casa_tools_config)
    logger.info("Registered casa-framework MCP tools")

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

    engagement_driver = InCasaDriver(
        topic_stream_factory=_topic_stream_factory,
        persist_session_id=engagement_registry.persist_session_id,
    )

    # claude_code driver still uses the buffered send_to_topic path
    # (different driver, separate streaming work parked in Phase 4 / E-12).
    async def _send_to_topic(thread_id: int, text: str) -> None:
        if telegram_channel is not None:
            await telegram_channel.send_to_topic(thread_id, text)

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
    try:
        await replay_undergoing_engagements(
            registry=engagement_registry,
            driver=claude_code_driver,
            executor_registry=executor_registry,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Plan 4a boot-replay failed — claude_code engagements may be "
            "in an inconsistent state: %s", exc,
        )

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

        async def _driver_send_user_turn(rec, text):
            if rec.driver == "claude_code":
                await claude_code_driver.send_user_turn(rec, text)
            else:
                await engagement_driver.send_user_turn(rec, text)
        telegram_channel._driver_send_user_turn = _driver_send_user_turn

        async def _finalize_cancel(rec, reason="user"):
            from tools import _finalize_engagement
            driver = (claude_code_driver if rec.driver == "claude_code"
                      else engagement_driver)
            await _finalize_engagement(
                rec, outcome="cancelled", text=f"Cancelled by {reason}.",
                artifacts=[], next_steps=[],
                driver=driver,
            )
        telegram_channel._finalize_cancel = _finalize_cancel

        async def _finalize_complete_user(rec):
            from tools import _finalize_engagement
            driver = (claude_code_driver if rec.driver == "claude_code"
                      else engagement_driver)
            await _finalize_engagement(
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

    # 13b. Orphan delegation recovery (Phase 3.1 §7.4). The bus loops are
    # up; the orphan NOTIFICATIONs we post here queue on Ellen's queue
    # and drain once she's processing messages. HTTP is already accepting
    # requests — that's fine, orphans are not racing anything user-facing.
    orphans = specialist_registry.orphans_from_disk()
    for record in orphans:
        target_role = record.origin.get("role") or assistant_role
        if target_role in bus.queues:
            synthetic = DelegationComplete(
                delegation_id=record.id,
                agent=record.agent,
                status="error",
                kind="restart_orphan",
                message="Lost on restart",
                origin=record.origin,
                elapsed_s=0.0,
            )
            await bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source=record.agent,
                target=target_role,
                content=synthetic,
                channel=record.origin.get("channel", ""),
                context={
                    "cid": record.origin.get("cid", "-"),
                    "chat_id": record.origin.get("chat_id", ""),
                    "delegation_id": record.id,
                },
            ))
            logger.warning(
                "Orphan delegation recovered: id=%s agent=%s — NOTIFICATION posted",
                record.id[:8], record.agent,
            )
        else:
            logger.error(
                "Orphan delegation %s targets unknown role %r — dropped",
                record.id[:8], target_role,
            )

    # 13c. Surface any default-sync overwrites to the operator (direct
    # telegram outbound — see notify_config_sync).
    await notify_config_sync(bus)
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

    # H10 (v0.49.0): include consumers spawned after boot by reload
    # (bus.start_agent_loop) — the local loop_tasks list only has the
    # boot-time ones. cancel() is idempotent for already-evicted tasks.
    all_loop_tasks = set(loop_tasks) | set(bus.agent_loop_tasks())
    for task in all_loop_tasks:
        task.cancel()
    await asyncio.gather(*all_loop_tasks, return_exceptions=True)

    await channel_manager.stop_all()
    # Close the shared Hindsight client session (L32) so aiohttp does not
    # warn about an unclosed session; no-op for NoOp/other backends.
    try:
        await semantic_memory.close()
    except Exception:  # noqa: BLE001
        logger.warning("semantic memory close failed", exc_info=True)
    for _r in runners:
        await _r.cleanup()
    logger.info("Casa core shutdown complete")


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
