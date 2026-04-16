"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import signal
import sys
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
from memory import HonchoMemoryProvider, MemoryProvider
from session_registry import SessionRegistry

logger = logging.getLogger(__name__)

CONFIG_DIR = "/addon_configs/casa-agent"
DATA_DIR = "/data"


# ------------------------------------------------------------------
# NoOp memory for when Honcho is not configured
# ------------------------------------------------------------------


class NoOpMemory(MemoryProvider):
    """Stub memory provider that does nothing."""

    async def get_context(
        self,
        peer_id: str,
        token_budget: int,
        exclude_tags: list[str] | None = None,
    ) -> str:
        return ""

    async def store_message(
        self,
        session_id: str,
        peer_id: str,
        content: str,
        role: str = "user",
        tags: list[str] | None = None,
    ) -> None:
        pass

    async def create_session(self, peer_id: str) -> str:
        return "noop"

    async def close_session(self, session_id: str) -> None:
        pass


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
    memory: MemoryProvider
    honcho_key = os.environ.get("HONCHO_API_KEY", "")
    if honcho_key:
        honcho_url = os.environ.get("HONCHO_API_URL", "https://api.honcho.dev")
        honcho = HonchoMemoryProvider(
            api_url=honcho_url,
            api_key=honcho_key,
        )
        await honcho.initialize()
        memory = honcho
        logger.info("Honcho memory provider initialized")
    else:
        memory = NoOpMemory()
        logger.info("No HONCHO_API_KEY set; using no-op memory")

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

    # 8. Load agent configs
    from agent import Agent

    agents: dict[str, Agent] = {}
    loop_tasks: list[asyncio.Task] = []

    # -- Ellen (primary agent) --
    ellen_config_path = os.path.join(CONFIG_DIR, "agents", "ellen.yaml")
    ellen_config: AgentConfig = load_agent_config(ellen_config_path)
    ellen_name = ellen_config.name.lower()

    ellen = Agent(
        config=ellen_config,
        memory=memory,
        session_registry=session_registry,
        mcp_registry=mcp_registry,
        channel_manager=channel_manager,
    )
    bus.register(ellen_name, ellen.handle_message)
    agents[ellen_name] = ellen
    logger.info("Agent '%s' registered on bus (model=%s)", ellen_name, ellen_config.model)

    # -- Tina (voice agent) --
    tina_config_path = os.path.join(CONFIG_DIR, "agents", "tina.yaml")
    if os.path.exists(tina_config_path):
        tina_config: AgentConfig = load_agent_config(tina_config_path)
        tina_name = tina_config.name.lower()

        tina = Agent(
            config=tina_config,
            memory=memory,
            session_registry=session_registry,
            mcp_registry=mcp_registry,
            channel_manager=channel_manager,
        )
        bus.register(tina_name, tina.handle_message)
        agents[tina_name] = tina
        logger.info("Agent '%s' registered on bus (model=%s)", tina_name, tina_config.model)
    else:
        tina_name = None
        logger.info("No tina.yaml found; voice agent not started")

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
            default_agent=ellen_name,
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
            target=ellen_name,
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

        agent_name = request.match_info.get("agent", ellen_name)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        prompt = payload.get("prompt", "")
        if not prompt:
            return web.json_response({"error": "missing 'prompt' field"}, status=400)

        msg = BusMessage(
            type=MessageType.REQUEST,
            source="webhook",
            target=agent_name,
            content=prompt,
            channel="webhook",
            context=payload.get("context", {}),
        )
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
        for name, agent in agents.items():
            # "claude-opus-4-6" → "Opus 4.6"
            model = agent.config.model.replace("claude-", "")
            parts = model.split("-")
            if len(parts) >= 3:
                model = f"{parts[0].capitalize()} {parts[1]}.{parts[2]}"
            agent_rows += _row(name.capitalize(), model)

        # Channel rows
        channel_rows = ""
        if telegram_channel is not None:
            tg_mode = "webhook" if telegram_channel._webhook_url else "polling"
            tg_delivery = telegram_channel._delivery_mode
            channel_rows += _row("Telegram", f"{tg_mode}, {tg_delivery}", "on")
        else:
            channel_rows += _row("Telegram", "not configured", "off")

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
    heartbeat_enabled = os.environ.get("HEARTBEAT_ENABLED", "true").lower() == "true"
    heartbeat_interval = int(os.environ.get("HEARTBEAT_INTERVAL_MINUTES", "60"))

    if os.path.exists(schedules_path):
        with open(schedules_path, "r", encoding="utf-8") as fh:
            schedules_data = yaml.safe_load(fh) or {}
        hb = schedules_data.get("heartbeat", {})
        heartbeat_enabled = hb.get("enabled", heartbeat_enabled)
        heartbeat_interval = hb.get("interval_minutes", heartbeat_interval)
        heartbeat_agent = hb.get("agent", ellen_name)
        heartbeat_prompt = hb.get("prompt", "Heartbeat: check for pending tasks.")
    else:
        heartbeat_agent = ellen_name
        heartbeat_prompt = "Heartbeat: check for pending tasks."

    if heartbeat_enabled and heartbeat_agent in agents:
        async def _heartbeat_tick() -> None:
            logger.info("Heartbeat firing for agent '%s'", heartbeat_agent)
            msg = BusMessage(
                type=MessageType.SCHEDULED,
                source="scheduler",
                target=heartbeat_agent,
                content=heartbeat_prompt,
                channel="",
                context={"trigger": "heartbeat"},
            )
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
