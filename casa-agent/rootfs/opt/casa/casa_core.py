"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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

import yaml
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig, load_agent_config
from log_redact import RedactingFilter
from mcp_registry import McpServerRegistry
from memory import (
    CachedMemoryProvider,
    HonchoMemoryProvider,
    MemoryProvider,
    NoOpMemory,
    SqliteMemoryProvider,
)
from session_registry import SessionRegistry

logger = logging.getLogger(__name__)

CONFIG_DIR = "/addon_configs/casa-agent"
DATA_DIR = "/data"


# ------------------------------------------------------------------
# Health endpoint
# ------------------------------------------------------------------


async def healthz(_request: web.Request) -> web.Response:
    """Return a simple health-check response."""
    return web.json_response({"status": "ok"})


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


def init_heartbeat_defaults(env: dict[str, str] | None = None) -> tuple[bool, int]:
    """Resolve heartbeat defaults from env. Never raises.

    Returns ``(enabled, interval_minutes)``. Called before the HTTP server
    starts so the dashboard closure has concrete bindings — otherwise a
    request landing between ``site.start()`` and the scheduler block raises
    ``UnboundLocalError`` on ``heartbeat_enabled`` / ``heartbeat_interval``.
    """
    env = env if env is not None else os.environ
    enabled = env.get("HEARTBEAT_ENABLED", "true").lower() == "true"
    try:
        interval = int(env.get("HEARTBEAT_INTERVAL_MINUTES", "60"))
    except ValueError:
        interval = 60
    if interval < 1:
        interval = 60
    return enabled, interval


def build_heartbeat_message(agent: str, prompt: str) -> BusMessage:
    """Build a scheduled heartbeat BusMessage.

    ``channel`` must be non-empty: ``Agent._process`` calls
    ``build_session_key(msg.channel, ...)`` which rejects empty channels.
    """
    return BusMessage(
        type=MessageType.SCHEDULED,
        source="scheduler",
        target=agent,
        content=prompt,
        channel="scheduler",
        context={"chat_id": "heartbeat", "trigger": "heartbeat"},
    )


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
    """
    context = dict(payload.get("context") or {})
    if not context.get("chat_id"):
        context["chat_id"] = str(uuid.uuid4())
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


def _load_agents_by_role(agents_dir: str) -> dict[str, AgentConfig]:
    """Scan *agents_dir* and return a dict keyed on role.

    Ignores ``subagents.yaml`` (multi-agent file for on-demand subagents;
    Phase 3+). Logs and skips files whose role is neither ``assistant``
    nor ``butler`` (Phase 4 will handle user-defined agents).
    """
    allowed_roles = {"assistant", "butler"}
    found: dict[str, AgentConfig] = {}
    if not os.path.isdir(agents_dir):
        return found
    for entry in sorted(os.listdir(agents_dir)):
        if not entry.endswith(".yaml") or entry == "subagents.yaml":
            continue
        path = os.path.join(agents_dir, entry)
        try:
            cfg = load_agent_config(path)
        except Exception as exc:
            logger.error("Failed to load %s: %s", path, exc)
            continue
        if cfg.role not in allowed_roles:
            logger.info(
                "Skipping %s (role=%r not in always-on set %s)",
                entry,
                cfg.role,
                sorted(allowed_roles),
            )
            continue
        if cfg.role in found:
            logger.error(
                "Duplicate role %r in %s (first seen earlier); skipping",
                cfg.role,
                entry,
            )
            continue
        found[cfg.role] = cfg
    return found


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


async def main() -> None:
    """Async entry point for the Casa add-on."""

    # 1. Logging (with secret redaction)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger().addFilter(RedactingFilter())
    # Quiet the httpx logger (Telegram polling produces a line every ~10s)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.info("Casa core starting up")

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
        base_memory = SqliteMemoryProvider(mem_choice.db_path)
        logger.info(
            "SQLite memory provider initialized (path=%s)", mem_choice.db_path,
        )
    else:  # noop
        base_memory = NoOpMemory()
        logger.info("MEMORY_BACKEND=noop; using no-op memory")

    # 3. Message bus
    bus = MessageBus()

    # 4. Session registry
    sessions_path = os.path.join(DATA_DIR, "sessions.json")
    session_registry = SessionRegistry(sessions_path)

    # 5. MCP server registry
    mcp_registry = McpServerRegistry()

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if supervisor_token:
        mcp_registry.register_http(
            name="homeassistant",
            url="http://supervisor/core/api/mcp",
            headers={"Authorization": f"Bearer {supervisor_token}"},
        )
        logger.info("Registered Home Assistant MCP server")

    # 6. Channel manager
    channel_manager = ChannelManager()

    # 7. Framework tools
    from tools import create_casa_tools, init_tools

    init_tools(channel_manager, bus)
    casa_tools_config = create_casa_tools()
    mcp_registry.register_sdk("casa-framework", casa_tools_config)
    logger.info("Registered casa-framework MCP tools")

    # 8. Load agent configs by role
    from agent import Agent

    agents_dir = os.path.join(CONFIG_DIR, "agents")
    role_configs = _load_agents_by_role(agents_dir)

    if "assistant" not in role_configs:
        raise RuntimeError(
            f"No agent with role 'assistant' found in {agents_dir}. "
            "Casa cannot start without a primary assistant. Check that "
            "assistant.yaml exists and its YAML includes `role: assistant`."
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
        )
        bus.register(role, agent.handle_message)
        agents[role] = agent
        logger.info(
            "Agent '%s' registered (name=%s, model=%s, memory=%s)",
            role,
            cfg.name,
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
        )
        channel_manager.register(telegram_channel)
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
            context={"webhook_name": name},
        )
        await bus.send(msg)
        return web.json_response({"status": "accepted"})

    async def invoke_handler(request: web.Request) -> web.Response:
        """Direct agent invocation (POST /invoke/{agent})."""
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

    # Heartbeat defaults must be resolved before `dashboard` is defined:
    # the HTTP server starts below before the scheduler block, and a
    # request landing in between would otherwise hit UnboundLocalError on
    # these closure variables. The scheduler block may still override them
    # from schedules.yaml.
    heartbeat_enabled, heartbeat_interval = init_heartbeat_defaults()
    heartbeat_agent = assistant_role
    heartbeat_prompt = "Heartbeat: check for pending tasks."

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
                agent.config.name
                if agent.config.name and not agent.config.name.startswith("${")
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
        mem_type = "Honcho" if os.environ.get("HONCHO_API_KEY") else "none"
        system_rows += _row("Memory", mem_type, "on" if mem_type != "none" else "off")
        system_rows += _row("Webhook auth", "enabled" if webhook_secret else "disabled",
                            "on" if webhook_secret else "off")
        hb_label = f"every {heartbeat_interval} min" if heartbeat_enabled else "disabled"
        system_rows += _row("Heartbeat", hb_label, "on" if heartbeat_enabled else "off")
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

    # 13. aiohttp app
    app = web.Application()
    if voice_channel is not None:
        voice_channel.register_routes(app)
    app.router.add_get("/", dashboard)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/webhook/{name}", webhook_handler)
    app.router.add_post("/invoke/{agent}", invoke_handler)
    app.router.add_post("/telegram/update", telegram_update_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    logger.info("HTTP server listening on 0.0.0.0:8099")

    # 12. Start all channels
    await channel_manager.start_all()

    # 13. Agent loop tasks
    for name in list(agents.keys()) + ["telegram"]:
        if name in bus.queues:
            loop_tasks.append(asyncio.create_task(bus.run_agent_loop(name)))

    # 14. APScheduler heartbeat
    scheduler = AsyncIOScheduler()

    schedules_path = os.path.join(CONFIG_DIR, "schedules.yaml")
    if os.path.exists(schedules_path):
        with open(schedules_path, "r", encoding="utf-8") as fh:
            schedules_data = yaml.safe_load(fh) or {}
        hb = schedules_data.get("heartbeat", {})
        heartbeat_enabled = hb.get("enabled", heartbeat_enabled)
        heartbeat_interval = hb.get("interval_minutes", heartbeat_interval)
        heartbeat_agent = hb.get("agent", heartbeat_agent)
        heartbeat_prompt = hb.get("prompt", heartbeat_prompt)

    if heartbeat_enabled and heartbeat_agent in agents:
        async def _heartbeat_tick() -> None:
            logger.info("Heartbeat firing for agent '%s'", heartbeat_agent)
            msg = build_heartbeat_message(heartbeat_agent, heartbeat_prompt)
            await bus.send(msg)

        scheduler.add_job(
            _heartbeat_tick,
            "interval",
            minutes=heartbeat_interval,
            id="heartbeat",
        )
        logger.info(
            "Heartbeat scheduled: every %d min -> %s",
            heartbeat_interval,
            heartbeat_agent,
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

    for task in loop_tasks:
        task.cancel()
    await asyncio.gather(*loop_tasks, return_exceptions=True)

    await channel_manager.stop_all()
    await runner.cleanup()
    logger.info("Casa core shutdown complete")


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
