"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import signal
import sqlite3
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
from log_cid import install_logging, new_cid
from casa_core_middleware import cid_middleware, CasaAccessLogger
from mcp_registry import McpServerRegistry
from memory import (
    CachedMemoryProvider,
    HonchoMemoryProvider,
    MemoryProvider,
    NoOpMemory,
    SqliteMemoryProvider,
)
from policies import load_policies
from session_registry import SessionRegistry
from session_sweeper import SessionSweeper
from rate_limit import RateLimiter, rate_limit_response
from timekeeping import resolve_tz
from trigger_registry import TriggerRegistry

logger = logging.getLogger(__name__)

CONFIG_DIR = "/addon_configs/casa-agent"
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
) -> "web.AppRunner":
    """Build and start a second aiohttp AppRunner bound to a Unix socket.

    Routes:
      POST /internal/tools/call    -> _make_internal_tools_call_handler(...)
      POST /internal/hooks/resolve -> _make_internal_hooks_resolve_handler(...)

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
        _make_internal_hooks_resolve_handler(hook_policies=hook_policies),
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


def _make_public_hooks_fallback_handler(*, hook_policies: dict):
    """Public-8099 /hooks/resolve handler.

    Same body shape as the internal handler ({"policy": ..., "payload": ...}).
    Just re-exports the internal factory under a different name for clarity
    at the call site — behavior is identical.
    """
    from internal_handlers import _make_internal_hooks_resolve_handler
    return _make_internal_hooks_resolve_handler(hook_policies=hook_policies)


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

    async with s6_rc._compile_lock:
        # 1. Orphan sweep — dirs for non-UNDERGOING engagements, remove them.
        removed_orphans = s6_rc.sweep_orphan_service_dirs(
            svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
            keep_engagement_ids=keep_ids,
        )

        # Fast path: no UNDERGOING engagements and no orphans were swept →
        # the engagement sources dir is empty and unchanged. Running
        # s6-rc-compile against an empty source dir prints
        # "source /data/casa-s6-services is empty" to stderr at every boot,
        # plus burns one compile + one s6-rc-update for nothing. Skip it.
        if not undergoing and not removed_orphans:
            return

        # 2. Heal missing service dirs for UNDERGOING engagements.
        for rec in undergoing:
            svc_dir = os.path.join(
                s6_rc.ENGAGEMENT_SOURCES_ROOT, f"engagement-{rec.id}",
            )
            if os.path.isdir(svc_dir):
                continue

            if executor_registry is None:
                logger.warning(
                    "boot replay: service dir missing for engagement %s "
                    "— no executor_registry passed; leaving UNDERGOING",
                    rec.id[:8],
                )
                continue

            defn = executor_registry.get(rec.role_or_type)
            if defn is None:
                logger.warning(
                    "boot replay: cannot heal engagement %s — executor type "
                    "%r not registered; leaving UNDERGOING",
                    rec.id[:8], rec.role_or_type,
                )
                continue

            # Re-render run + log scripts.
            run_script = render_run_script(
                engagement_id=rec.id,
                permission_mode=defn.permission_mode or "acceptEdits",
                extra_dirs=list(defn.extra_dirs or []),
            )
            log_script = render_log_run_script(engagement_id=rec.id)
            s6_rc.write_service_dir(
                svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                engagement_id=rec.id,
                run_script=run_script,
                depends_on=["init-setup-configs"],
                log_run_script=log_script,
            )
            # Ensure FIFO exists — it might have been wiped alongside the svc dir.
            fifo = os.path.join("/data/engagements", rec.id, "stdin.fifo")
            try:
                if os.path.isdir(os.path.dirname(fifo)) and not os.path.exists(fifo):
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

        # 3. Single compile + update pass.
        await s6_rc._compile_and_update_locked()

        # 4. Start each (idempotent under s6-rc change).
        for rec in undergoing:
            try:
                await s6_rc.start_service(engagement_id=rec.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "boot replay: start_service(%s) failed: %s",
                    rec.id[:8], exc,
                )

    # 5. Background tasks OUTSIDE the lock (long-lived).
    for rec in undergoing:
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

    When an executor defines non-default hook parameters (e.g.
    casa_config_guard.forbid_write_paths), the CC driver path currently
    uses the factory defaults. This is acceptable for v0.13.1 because the
    only in-tree executor-hooks config is the Configurator's, and its
    defaults match what the executor wants. Wiring per-executor params
    into the HTTP path is a later item.
    """
    cc_policies: dict = {}
    for name, entry in hook_policies.items():
        matcher = entry["matcher"]
        callback = entry["factory"]()  # default-configured HookCallback
        cc_policies[name] = (matcher, callback)
    return cc_policies


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

_VALID_MEMORY_BACKENDS = ("honcho", "sqlite", "noop")


from dataclasses import dataclass as _dataclass


@_dataclass
class _MemoryChoice:
    """Declarative memory-backend pick. ``main()`` turns this into a
    concrete ``MemoryProvider`` instance."""
    backend: str                       # honcho | sqlite | noop
    db_path: str = "/data/memory.sqlite"
    honcho_api_key: str = ""
    honcho_api_url: str = "https://api.honcho.dev"


def resolve_memory_backend_choice(env: dict[str, str]) -> _MemoryChoice:
    """Resolve ``MEMORY_BACKEND`` + keys into a backend choice.

    Order:
      1. Explicit ``MEMORY_BACKEND`` wins. ``honcho`` without
         ``HONCHO_API_KEY`` raises. Invalid values raise.
      2. Else ``HONCHO_API_KEY`` → ``honcho``.
      3. Else ``sqlite`` (fresh-install default).
    """
    backend = env.get("MEMORY_BACKEND", "").strip().lower()
    api_key = env.get("HONCHO_API_KEY", "")
    api_url = env.get("HONCHO_API_URL", "https://api.honcho.dev")
    db_path = env.get("MEMORY_DB_PATH", "/data/memory.sqlite")

    if backend:
        if backend not in _VALID_MEMORY_BACKENDS:
            raise ValueError(
                f"Invalid MEMORY_BACKEND={backend!r}; "
                f"must be one of {_VALID_MEMORY_BACKENDS}"
            )
        if backend == "honcho" and not api_key:
            raise ValueError(
                "MEMORY_BACKEND=honcho requires HONCHO_API_KEY to be set"
            )
        return _MemoryChoice(
            backend=backend, db_path=db_path,
            honcho_api_key=api_key, honcho_api_url=api_url,
        )

    if api_key:
        return _MemoryChoice(
            backend="honcho", db_path=db_path,
            honcho_api_key=api_key, honcho_api_url=api_url,
        )

    return _MemoryChoice(backend="sqlite", db_path=db_path)


def _wrap_memory_for_strategy(
    backend: MemoryProvider,
    role: str,
    strategy: str,
    sqlite_warning_emitted: list[bool],
) -> MemoryProvider:
    """Apply the per-agent ``read_strategy`` to *backend*.

    ``sqlite_warning_emitted`` is a one-element mutable flag used to
    emit the "SQLite backend — caching not applied" line at most once
    per process start (spec §2).
    """
    if strategy == "cached":
        if isinstance(backend, SqliteMemoryProvider):
            if not sqlite_warning_emitted[0]:
                logger.info(
                    "SQLite backend — caching not applied "
                    "(native reads are <1 ms)"
                )
                sqlite_warning_emitted[0] = True
            return backend
        return CachedMemoryProvider(backend)

    if strategy == "card_only":
        logger.warning(
            "Agent '%s' requests read_strategy=card_only which is not "
            "implemented in 2.2a; falling back to per_turn",
            role,
        )
        return backend

    # per_turn
    return backend


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
# Main
# ------------------------------------------------------------------


async def main() -> None:
    """Async entry point for the Casa add-on."""

    # 1. Logging (correlation ids + secret redaction, spec 5.2 §7).
    install_logging()
    logger.info("Casa core starting up")

    # 1a. §8: universal op:// resolution for password-typed addon options.
    # OP_SERVICE_ACCOUNT_TOKEN is already in env (exported by svc-casa/run from
    # the onepassword_service_account_token addon option). Resolve all
    # password-typed options in-place now, before any consumer reads them.
    from secrets_resolver import resolve as _resolve_secret
    _PASSWORD_ENV_VARS = (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "HONCHO_API_KEY",
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
    for _var, _value in _read_plugin_env().items():
        try:
            os.environ[_var] = _resolve_secret(_value)
        except RuntimeError as _exc:
            logger.warning(
                "plugin-env: %s unresolved: %s — plugin's MCP server will fail to start",
                _var, _exc,
            )

    # 2. Memory
    base_memory: MemoryProvider
    mem_choice = resolve_memory_backend_choice(dict(os.environ))
    if mem_choice.backend == "honcho":
        base_memory = HonchoMemoryProvider(
            api_url=mem_choice.honcho_api_url,
            api_key=mem_choice.honcho_api_key,
        )
        logger.info("Honcho v3 memory provider initialized")
    elif mem_choice.backend == "sqlite":
        try:
            base_memory = SqliteMemoryProvider(mem_choice.db_path)
            logger.info(
                "SQLite memory provider initialized (path=%s)", mem_choice.db_path,
            )
        except (sqlite3.OperationalError, OSError) as exc:
            logger.error(
                "SQLite memory init failed (path=%s): %s — degrading to no-op",
                mem_choice.db_path, exc,
            )
            base_memory = NoOpMemory()
    else:  # noop
        base_memory = NoOpMemory()
        logger.info("MEMORY_BACKEND=noop; using no-op memory")

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
    init_tools(
        channel_manager, bus, specialist_registry, mcp_registry,
        agent_role_map=_build_role_registry(
            residents=role_configs, specialists=specialist_configs,
        ),
        agent_registry=agent_registry,
        trigger_registry=trigger_registry,
        engagement_registry=engagement_registry,
        executor_registry=executor_registry,
    )
    casa_tools_config = create_casa_tools()
    mcp_registry.register_sdk("casa-framework", casa_tools_config)
    logger.info("Registered casa-framework MCP tools")

    # Plan 4b §5.1 — ensure every loaded in_casa agent has an agent-home with
    # default plugins seeded from plugins.yaml. Idempotent — runs every boot.
    from agent_home import provision_agent_home as _provision_agent_home
    _HOME_ROOT = Path("/addon_configs/casa-agent/agent-home")
    _DEFAULTS_ROOT = Path("/opt/casa")
    for _role in role_configs:
        try:
            _provision_agent_home(role=_role, home_root=_HOME_ROOT, defaults_root=_DEFAULTS_ROOT)
        except Exception as _exc:  # noqa: BLE001
            logger.warning("agent-home provisioning failed for role=%s: %s", _role, _exc)

    # 3.2: scope registry — loads scopes.yaml, embeds descriptions.
    from scope_registry import load_scope_library, ScopeRegistry
    scope_lib = load_scope_library(
        os.path.join(CONFIG_DIR, "policies", "scopes.yaml"),
    )
    scope_threshold = float(os.environ.get("CASA_SCOPE_THRESHOLD", "0.35"))
    scope_registry = ScopeRegistry(scope_lib, threshold=scope_threshold)
    await scope_registry.prepare()
    if scope_registry._degraded:
        logger.warning(
            "ScopeRegistry running in DEGRADED mode — fan-out to all readable scopes"
        )
    else:
        logger.info(
            "ScopeRegistry ready — embedded %d scopes (threshold=%.2f)",
            len(scope_lib.names()), scope_threshold,
        )

    if "assistant" not in role_configs:
        raise RuntimeError(
            f"No agent with role 'assistant' found in {agents_dir}. "
            "Casa cannot start without a primary assistant. Check that "
            "agents/assistant/ exists and runtime.yaml declares "
            "`role: assistant`."
        )

    agents: dict[str, Agent] = {}
    loop_tasks: list[asyncio.Task] = []

    sqlite_warning_emitted = [False]

    for role, cfg in role_configs.items():
        agent_memory = _wrap_memory_for_strategy(
            base_memory,
            role=role,
            strategy=cfg.memory.read_strategy,
            sqlite_warning_emitted=sqlite_warning_emitted,
        )

        agent = Agent(
            config=cfg,
            memory=agent_memory,
            session_registry=session_registry,
            mcp_registry=mcp_registry,
            channel_manager=channel_manager,
            scope_registry=scope_registry,
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
        )
        channel_manager.register(telegram_channel)
        await telegram_channel.setup_engagement_features()
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

    async def _send_to_topic(thread_id: int, text: str) -> None:
        if telegram_channel is not None:
            await telegram_channel.send_to_topic(thread_id, text)

    engagement_driver = InCasaDriver(send_to_topic=_send_to_topic)

    # Expose on the agent module so tools.emit_completion / cancel_engagement
    # can find it without circular imports.
    import agent as agent_mod
    agent_mod.active_engagement_driver = engagement_driver
    agent_mod.active_memory_provider = base_memory    # already constructed above
    agent_mod.active_executor_registry = executor_registry

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
        base_plugins_root="/opt/casa/claude-plugins/base",
        send_to_topic=_send_to_topic,
        casa_framework_mcp_url=_casa_framework_mcp_url,
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

    if telegram_channel is not None:
        telegram_channel._engagement_registry = engagement_registry
        telegram_channel._engagement_driver = engagement_driver
        telegram_channel._observer = observer

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
                driver=driver, memory_provider=base_memory,
            )
        telegram_channel._finalize_cancel = _finalize_cancel

        async def _finalize_complete_user(rec):
            from tools import _finalize_engagement
            driver = (claude_code_driver if rec.driver == "claude_code"
                      else engagement_driver)
            await _finalize_engagement(
                rec, outcome="completed", text="User-marked complete.",
                artifacts=[], next_steps=[],
                driver=driver, memory_provider=base_memory,
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
            memory=base_memory,
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

    def _verify_webhook(request: web.Request, body: bytes) -> bool:
        """Verify HMAC-SHA256 signature if a webhook secret is configured."""
        if not webhook_secret:
            return True
        sig = request.headers.get("X-Webhook-Signature", "")
        expected = hmac.new(
            webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    async def webhook_handler(request: web.Request) -> web.Response:
        """Handle named webhook invocations (POST /webhook/{name})."""
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        body = await request.read()
        if not _verify_webhook(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)

        name = request.match_info.get("name", "")
        try:
            payload = await request.json()
        except Exception:
            payload = body.decode("utf-8", errors="replace")

        msg = BusMessage(
            type=MessageType.SCHEDULED,
            source="webhook",
            target=assistant_role,
            content=f"Webhook '{name}' triggered with payload: {payload}",
            channel="webhook",
            context={"webhook_name": name, "cid": request["cid"]},
        )
        await bus.send(msg)
        return web.json_response({"status": "accepted"})

    async def invoke_handler(request: web.Request) -> web.Response:
        """Direct agent invocation (POST /invoke/{agent})."""
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        body = await request.read()
        if not _verify_webhook(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)

        agent_role = request.match_info.get("agent", assistant_role)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        prompt = payload.get("prompt", "")
        if not prompt:
            return web.json_response({"error": "missing 'prompt' field"}, status=400)

        payload.setdefault("context", {})["cid"] = request["cid"]
        msg = build_invoke_message(agent_role, prompt, payload)
        try:
            result = await bus.request(msg, timeout=300)
            return web.json_response({"response": str(result.content)})
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout"}, status=504)

    # 11. Telegram webhook route (only used when webhook_url is set)
    async def telegram_update_handler(request: web.Request) -> web.Response:
        """Receive Telegram updates pushed via webhook."""
        if telegram_channel is None:
            return web.json_response({"error": "telegram not configured"}, status=404)
        # Verify Telegram's secret token header
        if webhook_secret:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if token != webhook_secret:
                return web.Response(status=403)
        payload = await request.json()
        await telegram_channel.process_webhook_update(payload)
        return web.Response(status=200)

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
        mem_type = {
            "honcho": "Honcho",
            "sqlite": "SQLite",
            "noop": "none",
        }[mem_choice.backend]
        system_rows += _row(
            "Memory", mem_type, "on" if mem_choice.backend != "noop" else "off",
        )
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
    app.router.add_post(
        "/hooks/resolve",
        _make_public_hooks_fallback_handler(hook_policies=_cc_hook_policies),
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
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    logger.info("HTTP server listening on 0.0.0.0:8099")

    # Plan 4b/3.6: second AppRunner for the Unix-socket internal API
    # consumed by svc-casa-mcp. The same internal handlers are reused
    # in-process by the public-8099 fallback (route registrations above).
    from hooks import HOOK_POLICIES as _HOOK_POLICIES_FOR_INTERNAL
    _internal_hook_policies = _build_cc_hook_policies(_HOOK_POLICIES_FOR_INTERNAL)
    from tools import CASA_TOOLS as _CASA_TOOLS_FOR_INTERNAL
    _internal_tool_dispatch = {
        t.name: t.handler for t in _CASA_TOOLS_FOR_INTERNAL
    }
    internal_runner = await start_internal_unix_runner(
        socket_path="/run/casa/internal.sock",
        tool_dispatch=_internal_tool_dispatch,
        engagement_registry=engagement_registry,
        hook_policies=_internal_hook_policies,
    )
    # Track for shutdown.
    runners: list[web.AppRunner] = [runner, internal_runner]

    # 12. Start all channels
    await channel_manager.start_all()

    # 13. Agent loop tasks
    for name in list(agents.keys()) + ["telegram"]:
        if name in bus.queues:
            loop_tasks.append(asyncio.create_task(bus.run_agent_loop(name)))

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

    # 14. Kick off timers.
    session_sweeper.start()
    scheduler.add_job(
        lambda: asyncio.create_task(
            engagement_registry.sweep_idle_and_suspend(driver=engagement_driver)
        ),
        trigger="cron",
        id="engagement_idle_sweep",
        hour=8, minute=0,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # Plan 4a.1 §8: workspace sweeper — every 6 hours, removes terminal
    # engagement workspaces past retention.
    from drivers.workspace import _sweep_workspaces as _sweep_ws
    scheduler.add_job(
        lambda: asyncio.create_task(
            _sweep_ws(engagements_root="/data/engagements")
        ),
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

    for task in loop_tasks:
        task.cancel()
    await asyncio.gather(*loop_tasks, return_exceptions=True)

    await channel_manager.stop_all()
    for _r in runners:
        await _r.cleanup()
    logger.info("Casa core shutdown complete")


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
