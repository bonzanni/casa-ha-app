"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

from aiohttp import web

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig, load_agent_config
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

    # 1. Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
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

    # 8. Load Ellen config
    ellen_config_path = os.path.join(CONFIG_DIR, "agents", "ellen.yaml")
    ellen_config: AgentConfig = load_agent_config(ellen_config_path)
    logger.info("Loaded agent config: %s", ellen_config.name)

    # 9. Create agent and register on bus
    from agent import Agent

    agent = Agent(
        config=ellen_config,
        memory=memory,
        session_registry=session_registry,
        mcp_registry=mcp_registry,
        channel_manager=channel_manager,
    )
    agent_name = ellen_config.name.lower()
    bus.register(agent_name, agent.handle_message)
    logger.info("Agent '%s' registered on bus", agent_name)

    # 10. Telegram channel
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        from channels.telegram import TelegramChannel

        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        telegram_channel = TelegramChannel(
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            default_agent=agent_name,
            bus=bus,
        )
        channel_manager.register(telegram_channel)
        logger.info("Telegram channel registered (chat_id=%s)", telegram_chat_id)

    # 11. Register "telegram" as a bus target for outbound routing
    async def _telegram_outbound(msg: BusMessage) -> None:
        ch = channel_manager.get("telegram")
        if ch is not None:
            await ch.send(str(msg.content), msg.context)

    bus.register("telegram", _telegram_outbound)

    # 12-13. aiohttp app + start on 0.0.0.0:8099
    app = web.Application()
    app.router.add_get("/healthz", healthz)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    logger.info("HTTP server listening on 0.0.0.0:8099")

    # 14. Start all channels
    await channel_manager.start_all()

    # 15. Agent loop tasks
    loop_tasks: list[asyncio.Task] = []
    loop_tasks.append(asyncio.create_task(bus.run_agent_loop(agent_name)))
    loop_tasks.append(asyncio.create_task(bus.run_agent_loop("telegram")))

    # 16. Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    # On Windows, signal handlers are limited; wrap in try/except
    try:
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
    except NotImplementedError:
        # Windows does not support add_signal_handler for SIGTERM
        logger.warning("Signal handlers not supported on this platform")

    # 17. Wait for stop
    logger.info("Casa core running -- waiting for shutdown signal")
    await stop_event.wait()

    # 18. Cleanup
    logger.info("Shutting down...")
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
