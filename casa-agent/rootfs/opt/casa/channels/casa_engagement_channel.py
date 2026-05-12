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
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Literal, Union

import aiohttp
import anyio
from aiohttp import UnixConnector
from mcp.server.fastmcp import FastMCP
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.types import ClientNotification, Notification
from pydantic import RootModel

logger = logging.getLogger(__name__)


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

    Phase 2 (v0.37.0) declares both ``claude/channel`` (outbound: reply/ask/
    set_progress tools and topic-state notifications) and
    ``claude/channel/permission`` (inbound: ``notifications/claude/channel/
    permission_request`` → outbound: ``notifications/claude/channel/permission``
    verdict relay).
    """
    return {
        "claude/channel": {},
        "claude/channel/permission": {},
    }


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
# Permission relay (U1) — Phase 2
# ---------------------------------------------------------------------------

# Telegram inline-button callback_data byte limit (§9 of the spec; also asserted
# by Bot API docs).
_CALLBACK_DATA_MAX = 64


def _build_perm_buttons(request_id: str) -> list[list[dict[str, str]]]:
    """Build the [✅ Allow][❌ Deny] inline-keyboard row for a permission prompt.

    Raises ``ValueError`` if any callback_data would exceed Telegram's 64-byte
    limit (e.g. a pathologically long ``request_id``). The caller is expected
    to keep ``request_id`` short (uuid4 hex = 32 bytes → max 43-byte cd).
    """
    allow_cd = f"perm:allow:{request_id}"
    deny_cd = f"perm:deny:{request_id}"
    for cd in (allow_cd, deny_cd):
        if len(cd.encode("utf-8")) > _CALLBACK_DATA_MAX:
            raise ValueError(
                f"callback_data {cd!r} exceeds {_CALLBACK_DATA_MAX} bytes",
            )
    return [[
        {"text": "✅ Allow", "callback_data": allow_cd},
        {"text": "❌ Deny", "callback_data": deny_cd},
    ]]


async def handle_permission_request(params: dict[str, Any]) -> None:
    """U1: render the operator-facing permission prompt for a tool call.

    Receives ``notifications/claude/channel/permission_request`` params:
    ``{request_id, tool_name, description?, input_preview?}``. Renders an
    inline-keyboard prompt in the engagement topic via casa-main; the verdict
    travels back via ``CallbackQueryHandler`` → ``permission_verdict`` queue →
    long-poll drain → ``notifications/claude/channel/permission`` (delivered
    elsewhere in Phase 2 Task 21).
    """
    rid = params["request_id"]
    tool = params.get("tool_name", "(unknown)")
    desc = params.get("description") or ""
    preview = params.get("input_preview") or ""

    body_lines: list[str] = [f"Claude wants to use: *{tool}*"]
    if desc:
        body_lines.append("")
        body_lines.append(desc)
    if preview:
        body_lines.append("")
        body_lines.append(f"```\n{preview}\n```")
    body = "\n".join(body_lines)

    buttons = _build_perm_buttons(rid)
    await _internal_post(
        "/internal/channel/post_inline_keyboard",
        {
            "request_id": rid,
            "text": body,
            "buttons": buttons,
            "parse_mode": "MarkdownV2",
        },
    )


class PermissionRequestNotification(
    Notification[dict, Literal["notifications/claude/channel/permission_request"]]
):
    """Custom MCP notification declared by ``claude/channel/permission``.

    The CLI sends this when a tool call hits a permission gate that should
    relay through the channel UI instead of being auto-resolved by
    ``permission_mode``. Params shape per §9 of the spec:
    ``{request_id, tool_name, description?, input_preview?}``.
    """

    method: Literal["notifications/claude/channel/permission_request"] = (
        "notifications/claude/channel/permission_request"
    )
    params: dict


def _build_wider_notification_root_model() -> type[RootModel]:
    """Build a ``RootModel[Union[<existing>, PermissionRequestNotification]]``.

    Extends ``ClientNotification``'s root union so the BaseSession message loop
    validates and dispatches our custom notification class instead of warn-and-
    dropping it.
    """
    base_union = ClientNotification.model_fields["root"].annotation
    return RootModel[Union[base_union, PermissionRequestNotification]]


async def _on_permission_request_notification(
    notification: PermissionRequestNotification,
) -> None:
    """Bridge from the inner Server's notification dispatch to our handler."""
    await handle_permission_request(dict(notification.params))


def _register_permission_notification_handler() -> None:
    """Install the permission-request handler on the inner low-level Server.

    Idempotent — calling more than once just overwrites the same entry.
    """
    low_level = _resolve_mcp_server()
    if low_level is None:  # pragma: no cover — defensive only
        logger.warning(
            "no inner mcp Server available — permission notification disabled",
        )
        return
    low_level.notification_handlers[PermissionRequestNotification] = (
        _on_permission_request_notification
    )


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


_WIDER_NOTIFICATION_ROOT_MODEL: type[RootModel] | None = None


def _widen_session_notification_type() -> None:
    """Monkey-patch ``ServerSession.__init__`` so its message loop accepts our
    custom ``PermissionRequestNotification`` instead of warn-and-dropping it.

    The upstream ``ClientNotification`` union is closed — adding new methods is
    not exposed as a public hook in this mcp version (see §A.6.1 findings).
    We patch once per process; subsequent calls are no-ops. We only ever spawn
    one ServerSession per stdio process, so the patch's blast radius is
    self-contained.
    """
    global _WIDER_NOTIFICATION_ROOT_MODEL
    if _WIDER_NOTIFICATION_ROOT_MODEL is not None:
        return
    _WIDER_NOTIFICATION_ROOT_MODEL = _build_wider_notification_root_model()

    original_init = ServerSession.__init__

    def patched_init(self, *args, **kwargs):  # noqa: ANN001 — mirror upstream
        original_init(self, *args, **kwargs)
        # Widen the receive type so notifications/claude/channel/* parse instead
        # of being dropped as unknown-method validation failures.
        self._receive_notification_type = _WIDER_NOTIFICATION_ROOT_MODEL

    ServerSession.__init__ = patched_init  # type: ignore[method-assign]


async def _run_with_channel_capabilities() -> None:
    """Run the FastMCP server over stdio with channel experimental capabilities.

    Replaces ``FastMCP.run_stdio_async`` because the upstream coroutine
    hard-codes a no-arg ``create_initialization_options()`` call and we need
    to inject ``experimental_capabilities`` so the CLI sees ``claude/channel``.
    Additionally registers the permission-request notification handler and
    widens session-level notification parsing (see ``_widen_session_notification_type``).
    """
    low_level = _resolve_mcp_server()
    if low_level is None:
        raise RuntimeError(
            "FastMCP did not expose an underlying mcp Server "
            "(neither _mcp_server nor server attribute present)",
        )
    _widen_session_notification_type()
    _register_permission_notification_handler()
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
