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

    # 9. Telegram channel
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_channel = None
    if telegram_token:
        from channels.telegram import TelegramChannel

        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        telegram_webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
        telegram_channel = TelegramChannel(
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            default_agent=ellen_name,
            bus=bus,
            webhook_url=telegram_webhook_url,
        )
        channel_manager.register(telegram_channel)
        mode = "webhook" if telegram_webhook_url else "polling"
        logger.info("Telegram channel registered (%s, chat_id=%s)", mode, telegram_chat_id)

    # Register "telegram" as a bus target for outbound routing
    async def _telegram_outbound(msg: BusMessage) -> None:
        ch = channel_manager.get("telegram")
        if ch is not None:
            await ch.send(str(msg.content), msg.context)

    bus.register("telegram", _telegram_outbound)

    # 10. Webhook endpoints
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")

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
        payload = await request.json()
        await telegram_channel.process_webhook_update(payload)
        return web.Response(status=200)

    # 12. aiohttp app
    app = web.Application()
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
