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


async def _post_state_transition(new_state: str) -> None:
    """Best-effort POST /internal/channel/update_state.

    Failures are logged but never propagate — a transient unavailability of
    the casa-main socket must not stall the per-engagement MCP server's tool
    dispatch.
    """
    try:
        await _internal_post(
            "/internal/channel/update_state",
            {"new_state": new_state},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "update_state(%s) failed: %s", new_state, exc,
        )


class PermissionVerdictNotification(
    Notification[dict, Literal["notifications/claude/channel/permission"]]
):
    """Outbound (server → CLI) notification carrying the operator's verdict.

    Sent in response to a ``permission_request`` whose ``request_id`` matches.
    Params shape: ``{request_id, verdict}`` where ``verdict ∈ {"allow", "deny"}``.
    BaseSession.send_notification only calls ``.model_dump()`` on this object —
    there's no validation against ServerNotification's union, so this custom
    class round-trips through stdio as plain JSON-RPC.
    """

    method: Literal["notifications/claude/channel/permission"] = (
        "notifications/claude/channel/permission"
    )
    params: dict


def _build_wider_notification_root_model() -> type[ClientNotification]:
    """Build a ``ClientNotification`` subclass with a widened root union.

    Returning a ``ClientNotification`` *subclass* (not a free ``RootModel``)
    is load-bearing: ``Server._handle_message`` routes notifications with
    ``case types.ClientNotification(root=notify):``. A free
    ``RootModel[Union[...]]`` parses validation but fails the isinstance
    relationship — the message would be silently dropped at dispatch. The
    subclass widens the union *and* keeps the isinstance edge intact.

    Verified live 2026-05-12 on N150: stdio probe sent
    ``notifications/claude/channel/permission_request`` and observed
    ``mcp.server.lowlevel.server`` log line
    ``Received message: root=PermissionRequestNotification(...)`` followed
    by silent drop — confirming the bug before this fix.
    """
    base_union = ClientNotification.model_fields["root"].annotation

    class ChannelClientNotification(ClientNotification):
        root: Union[base_union, PermissionRequestNotification]  # type: ignore[valid-type]

    return ChannelClientNotification


# ---------------------------------------------------------------------------
# Permission verdict drain + outbound notification (Task 21)
# ---------------------------------------------------------------------------

# Set by the patched ServerSession.__init__; consumed by
# ``_emit_permission_notification`` to issue ``notifications/claude/channel/permission``.
_CURRENT_SESSION: Any | None = None

# How long each long-poll waits before returning empty + retrying. Kept shorter
# than the casa-main side's default (25s) so the channel server can quickly
# notice if the connection drops.
_PERMISSION_POLL_TIMEOUT_S = 25.0

# Backoff after an unexpected drain failure (e.g. socket disappeared during
# rebuild). Capped at ``_PERMISSION_DRAIN_BACKOFF_MAX`` so we don't busy-spin.
_PERMISSION_DRAIN_BACKOFF_INITIAL = 1.0
_PERMISSION_DRAIN_BACKOFF_MAX = 30.0


async def _emit_permission_notification(payload: dict[str, Any]) -> None:
    """Send ``notifications/claude/channel/permission`` to Claude carrying the
    verdict that the operator delivered via Telegram.

    ``payload`` shape (from ``_PERMISSION_QUEUES`` drain):
    ``{request_id, verdict, operator_id?}``. ``operator_id`` is dropped from
    the wire envelope — Claude only needs the verdict + request_id to resume.
    """
    session = _CURRENT_SESSION
    if session is None:
        logger.error(
            "no live session — verdict %s lost (request_id=%s)",
            payload.get("verdict"), payload.get("request_id"),
        )
        return

    notification = PermissionVerdictNotification(
        params={
            "request_id": payload["request_id"],
            "verdict": payload["verdict"],
        },
    )
    try:
        await session.send_notification(notification)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "send_notification failed for permission verdict (rid=%s): %s",
            payload.get("request_id"), exc,
        )
        return

    # U3 state flip back to active — best-effort. Runs AFTER the notification
    # so a transient socket hiccup on the state-update path doesn't delay
    # the verdict reaching Claude (which is what unblocks the engagement).
    await _post_state_transition("active")


async def _drain_permission_verdicts() -> None:
    """Long-poll casa-main's verdict queue and emit each verdict to Claude.

    Started by ``main()`` as an asyncio background task. Exits cleanly on
    ``CancelledError``; all other exceptions log + back off so a transient
    socket failure during casa-main reconnect does not kill the drain loop.
    """
    if ENGAGEMENT_ID is None:  # pragma: no cover — defensive
        logger.error("permission drain not started: ENGAGEMENT_ID unset")
        return

    backoff = _PERMISSION_DRAIN_BACKOFF_INITIAL
    while True:
        try:
            connector = UnixConnector(path=INTERNAL_SOCKET)
            async with aiohttp.ClientSession(connector=connector) as sess:
                async with sess.get(
                    f"http://localhost/internal/channel/permission_pending"
                    f"?engagement_id={ENGAGEMENT_ID}"
                    f"&timeout_s={int(_PERMISSION_POLL_TIMEOUT_S)}",
                    timeout=aiohttp.ClientTimeout(
                        total=_PERMISSION_POLL_TIMEOUT_S + 5,
                    ),
                ) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
            backoff = _PERMISSION_DRAIN_BACKOFF_INITIAL
            if not payload:
                continue  # idle long-poll cycle, no verdict ready
            await _emit_permission_notification(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "permission drain failure: %s — sleeping %.1fs",
                exc, backoff,
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _PERMISSION_DRAIN_BACKOFF_MAX)


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


_WIDER_NOTIFICATION_ROOT_MODEL: type[ClientNotification] | None = None


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
        global _CURRENT_SESSION
        original_init(self, *args, **kwargs)
        # Widen the receive type so notifications/claude/channel/* parse instead
        # of being dropped as unknown-method validation failures.
        self._receive_notification_type = _WIDER_NOTIFICATION_ROOT_MODEL
        # Stash the session so _emit_permission_notification can issue outbound
        # notifications/claude/channel/permission without re-resolving it.
        _CURRENT_SESSION = self

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
    async with stdio_server() as (read_stream, write_stream):
        drain_task = asyncio.create_task(
            _drain_permission_verdicts(), name="permission-drain",
        )
        try:
            await low_level.run(
                read_stream,
                write_stream,
                low_level.create_initialization_options(
                    experimental_capabilities=declared_capabilities(),
                ),
            )
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass


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
