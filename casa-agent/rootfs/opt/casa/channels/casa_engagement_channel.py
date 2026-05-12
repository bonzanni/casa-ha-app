"""casa-engagement-channel: per-engagement stdio MCP server (v0.37.0 Phase 1).

Launched by the ``claude_code`` driver via ``--channels server:<name>`` on the
``claude`` CLI subprocess. Supplies operator-facing tools that route through
casa-main's Unix-socket internal handler at ``/internal/channel/send_to_topic``,
which fans out to Telegram (or any future channel) by topic.

Phase 1 surface (this module):
- ``reply(chat_id, text)`` — append a message to the engagement's operator topic.
  ``chat_id`` is accepted for SDK compatibility but NEVER forwarded (D2 locked).
- ``declared_capabilities()`` — returns ``{"claude/channel": {}}`` so the
  MCP InitializationOptions advertise the channel capability to the CLI.

Phase 2 adds ``ask`` + ``set_progress`` and the permission relay; Phase 3 lands
the full INSTRUCTIONS prompt and any required state machine.

Implementation notes (see §A.6.1 of the build-time findings spec):
- ``FastMCP`` does NOT expose ``capabilities.experimental`` as a settable
  attribute; ``instructions`` is a read-only property. We pass instructions via
  the constructor kwarg and inject ``experimental_capabilities`` by calling
  ``server._mcp_server.create_initialization_options(...)`` from our own
  stdio bootstrap (replacing ``FastMCP.run_stdio_async`` which hard-codes a
  no-arg call).
- ``_mcp_server`` is an undocumented attribute. A defensive
  ``_resolve_mcp_server()`` helper falls back to ``server.server`` so a future
  mcp rename does not break us.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import aiohttp
import anyio
from aiohttp import UnixConnector
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server


# ---------------------------------------------------------------------------
# Module-level state populated from argv / env at import or main().
# ---------------------------------------------------------------------------

ENGAGEMENT_ID: str | None = None
INTERNAL_SOCKET: str = os.environ.get("CASA_INTERNAL_SOCKET", "/run/casa/internal.sock")

INSTRUCTIONS = (
    "Casa engagement channel (Phase 1 skeleton). The full operator-facing "
    "instructions prompt lands in Phase 3 (Task 29)."
)

server: FastMCP = FastMCP("casa-engagement-channel", instructions=INSTRUCTIONS)


def declared_capabilities() -> dict[str, dict]:
    """Experimental capabilities advertised in MCP InitializationOptions.

    Phase 1 declares only ``claude/channel``. Phase 2 will add
    ``claude/channel/permission`` once the permission relay lands.
    """
    return {"claude/channel": {}}


# ---------------------------------------------------------------------------
# Internal HTTP-over-Unix-socket client.
# ---------------------------------------------------------------------------

async def _internal_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST ``payload`` to ``path`` on the casa-main internal Unix socket.

    Auto-merges ``engagement_id=ENGAGEMENT_ID`` if not already present in
    ``payload``. Retries with exponential backoff: 3 attempts at 0.5s / 1s / 2s.
    Returns the parsed JSON response body on success; raises the last
    ``aiohttp`` exception if all attempts fail.
    """
    body = dict(payload)
    if "engagement_id" not in body and ENGAGEMENT_ID is not None:
        body["engagement_id"] = ENGAGEMENT_ID

    connector = UnixConnector(path=INTERNAL_SOCKET)
    delays = (0.5, 1.0, 2.0)
    last_exc: BaseException | None = None
    async with aiohttp.ClientSession(connector=connector) as session:
        for attempt, delay in enumerate(delays):
            try:
                async with session.post(
                    f"http://localhost{path}", json=body,
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, OSError) as exc:
                last_exc = exc
                if attempt < len(delays) - 1:
                    await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Tools.
# ---------------------------------------------------------------------------

@server.tool()
async def reply(chat_id: str, text: str) -> dict[str, Any]:
    """Append ``text`` to this engagement's operator topic.

    D2 locked: ``chat_id`` is accepted for CLI/SDK schema compatibility but is
    NEVER forwarded. The casa-main handler resolves the target chat/topic from
    ``engagement_id``, which we pass explicitly so the outbound payload is
    self-describing (auditable in logs / test capture-fakes).
    """
    del chat_id  # D2: explicitly discarded.
    payload: dict[str, Any] = {"text": text}
    if ENGAGEMENT_ID is not None:
        payload["engagement_id"] = ENGAGEMENT_ID
    return await _internal_post("/internal/channel/send_to_topic", payload)


# ---------------------------------------------------------------------------
# Test helpers (public API used by tests/test_casa_engagement_channel.py).
# ---------------------------------------------------------------------------

async def _list_tools_for_tests() -> list:
    """Test-only helper — DO NOT call from runtime tool-dispatch flow.

    Return the list of registered tools (each has a ``.name`` attribute).
    """
    return await server.list_tools()


async def _invoke_tool_for_tests(name: str, args: dict[str, Any]) -> Any:
    """Test-only helper — DO NOT call from runtime tool-dispatch flow.

    Invoke the registered tool function directly with ``**args``. Bypasses
    FastMCP's wire-format wrapping so tests can assert on the raw Python
    return value. Uses the ``_tool_manager`` accessor; falls back to
    ``call_tool`` only when the ``_tool_manager`` attr is missing entirely
    (a future mcp rename). When ``_tool_manager`` exists but the requested
    tool is not registered, raises ``KeyError`` rather than silently routing
    through ``call_tool``.
    """
    tm = getattr(server, "_tool_manager", None)
    if tm is not None:
        tool = tm.get_tool(name)
        if tool is None:
            raise KeyError(f"tool {name!r} not registered")
        fn = tool.fn
        result = fn(**args)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    return await server.call_tool(name, args)


# ---------------------------------------------------------------------------
# Stdio bootstrap with injected experimental capabilities (§A.6.1).
# ---------------------------------------------------------------------------

def _resolve_mcp_server():
    """Return the underlying low-level mcp Server, tolerating a future rename."""
    return getattr(server, "_mcp_server", None) or getattr(server, "server", None)


async def _run_with_channel_capabilities() -> None:
    """Run the FastMCP server over stdio with channel experimental capabilities.

    Replaces ``FastMCP.run_stdio_async`` because the upstream coroutine
    hard-codes a no-arg ``create_initialization_options()`` call and we need
    to inject ``experimental_capabilities`` so the CLI sees ``claude/channel``.
    """
    low_level = _resolve_mcp_server()
    if low_level is None:
        raise RuntimeError(
            "FastMCP did not expose an underlying mcp Server "
            "(neither _mcp_server nor server attribute present)",
        )
    async with stdio_server() as (read_stream, write_stream):
        await low_level.run(
            read_stream,
            write_stream,
            low_level.create_initialization_options(
                experimental_capabilities=declared_capabilities(),
            ),
        )


# ---------------------------------------------------------------------------
# CLI entry.
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="casa-engagement-channel")
    parser.add_argument(
        "--engagement-id",
        required=True,
        help="Engagement id used to route /internal/channel/* calls.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    global ENGAGEMENT_ID
    args = _build_arg_parser().parse_args(argv)
    ENGAGEMENT_ID = args.engagement_id
    anyio.run(_run_with_channel_capabilities)


# ---------------------------------------------------------------------------
# Test-only seam: populate module state directly without entering run loop.
# ---------------------------------------------------------------------------

def _configure_for_test(engagement_id: str, *, internal_socket: str | None = None) -> None:
    """Test-only seam — populate module state directly without entering run-loop.

    Production code never calls this; tests use it after re-importing the module
    with a fresh argv patch.
    """
    global ENGAGEMENT_ID, INTERNAL_SOCKET
    ENGAGEMENT_ID = engagement_id
    if internal_socket is not None:
        INTERNAL_SOCKET = internal_socket


if __name__ == "__main__":  # pragma: no cover
    main()
