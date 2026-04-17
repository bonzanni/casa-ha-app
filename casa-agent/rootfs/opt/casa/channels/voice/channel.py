"""VoiceChannel — dual-ingress (SSE + WS) channel for the butler agent.

Unlike Telegram, VoiceChannel does not own an IO loop. It mounts HTTP
routes on the aiohttp app already created by casa_core.py. The Channel
start()/stop() hooks exist for lifecycle (sweeper task), not transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from agent import _classify_error
from bus import BusMessage, MessageBus, MessageType
from channels import Channel
from channels.voice.prosodic import ProsodicSplitter
from channels.voice.session import VoiceSessionPool
from channels.voice.tts_adapter import TagDialectAdapter
from memory import MemoryProvider

logger = logging.getLogger(__name__)

_DEFAULT_ERROR_LINES = {
    "timeout":       "[flat] That took too long.",
    "rate_limit":    "[flat] I'm busy — try again shortly.",
    "sdk_error":     "[flat] I couldn't reach my brain.",
    "memory_error":  "",
    "channel_error": "[flat] Something went wrong.",
    "unknown":       "[flat] Sorry, something went wrong.",
}


class VoiceChannel(Channel):
    name: str = "voice"

    def __init__(
        self,
        bus: MessageBus,
        default_agent: str,
        webhook_secret: str,
        sse_path: str,
        ws_path: str,
        agent_configs: Mapping[str, Any],
        memory: MemoryProvider,
        idle_timeout: int,
        sse_enabled: bool = True,
        ws_enabled: bool = True,
    ) -> None:
        self._bus = bus
        self.default_agent = default_agent
        self._webhook_secret = webhook_secret
        self._sse_path = sse_path
        self._ws_path = ws_path
        self._sse_enabled = sse_enabled
        self._ws_enabled = ws_enabled
        self._agent_configs = agent_configs
        self._memory = memory
        self.pool = VoiceSessionPool(idle_timeout=idle_timeout)
        self._sweeper: asyncio.Task | None = None

    # --- Channel ABC --------------------------------------------------

    async def start(self) -> None:
        self._sweeper = asyncio.create_task(self.pool.run_sweeper())
        logger.info(
            "Voice channel active (sse=%s, ws=%s, sse_path=%s, ws_path=%s)",
            self._sse_enabled, self._ws_enabled, self._sse_path, self._ws_path,
        )

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()

    async def send(self, message: str, context: dict) -> None:
        # Voice has no out-of-band send path — responses are delivered
        # inline on the request's transport. No-op for the ChannelManager's
        # outbound registration (kept so the Channel ABC is satisfied).
        return None

    # --- create_on_token: adapter for the production Agent path -------

    def create_on_token(self, context: dict) -> Callable[[str], Awaitable[None]]:
        """Return the per-utterance streaming callback stored in context.

        The SSE/WS handler stashes the real callback in
        ``context["_on_token"]`` before dispatching on the bus. The
        production ``Agent._process`` resolves it via
        ``channel_manager.get("voice").create_on_token(msg.context)`` —
        so both the test stub (which reads context directly) and the
        production agent (which goes through this method) converge on
        the same callable.
        """
        cb = context.get("_on_token")
        if cb is None:
            async def _noop(_text: str) -> None:
                return None
            return _noop
        return cb

    # --- Route registration -------------------------------------------

    def register_routes(self, app: web.Application) -> None:
        if self._sse_enabled:
            app.router.add_post(self._sse_path, self._sse_handler)
        if self._ws_enabled:
            app.router.add_get(self._ws_path, self._ws_handler)

    # --- HMAC ---------------------------------------------------------

    def _verify(self, request: web.Request, body: bytes) -> bool:
        if not self._webhook_secret:
            return True
        sig = request.headers.get("X-Webhook-Signature", "")
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    # --- SSE ----------------------------------------------------------

    async def _sse_handler(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        if not self._verify(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        prompt = payload.get("prompt") or ""
        if not prompt:
            return web.json_response({"error": "missing 'prompt'"}, status=400)

        agent_role = payload.get("agent_role", self.default_agent)
        cfg = self._agent_configs.get(agent_role)
        if cfg is None:
            return web.json_response({"error": "unknown agent_role"}, status=404)

        scope_id = self._resolve_scope_id(payload)
        self.pool.ensure(scope_id)
        self.pool.touch(scope_id)
        utterance_id = str(uuid.uuid4())

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        splitter = ProsodicSplitter()
        adapter = TagDialectAdapter(cfg.tts.tag_dialect)
        last_text = ""

        async def on_token(accumulated: str) -> None:
            nonlocal last_text
            delta = accumulated[len(last_text):]
            last_text = accumulated
            for block in splitter.feed(delta):
                await _write_sse(response, "block", {
                    "text": adapter.render(block),
                    "final": False,
                })

        error_emitted = False

        async def _error_sink(kind: str, spoken: str) -> None:
            nonlocal error_emitted
            await _write_sse(response, "error", {
                "kind": kind, "spoken": spoken,
            })
            error_emitted = True

        msg = BusMessage(
            type=MessageType.REQUEST,
            source="voice",
            target=agent_role,
            content=prompt,
            channel="voice",
            context={
                "chat_id": scope_id,
                "utterance_id": utterance_id,
                **(payload.get("context") or {}),
                "_on_token": on_token,
                "_error_sink": _error_sink,
            },
        )

        try:
            result = await self._bus.request(msg, timeout=300)
            if error_emitted:
                return response
            tail = splitter.flush_tail()
            if tail:
                await _write_sse(response, "block", {
                    "text": adapter.render(tail),
                    "final": True,
                })
            await _write_sse(response, "done", {})
        except asyncio.CancelledError:
            # Client disconnect mid-stream — do NOT emit `event: done`.
            # Pool entry already created above, stays alive per spec §10.3.
            raise
        except Exception as exc:
            line = self._error_line(cfg, exc)
            await _write_sse(response, "error", {
                "kind": _classify_error(exc).value,
                "spoken": adapter.render(line) if line else "",
            })

        return response

    # --- helpers ------------------------------------------------------

    @staticmethod
    def _resolve_scope_id(payload: dict) -> str:
        if payload.get("scope_id"):
            return payload["scope_id"]
        ctx = payload.get("context") or {}
        return (
            ctx.get("user_id")
            or ctx.get("device_id")
            or ctx.get("conversation_id")
            or "anon"
        )

    @staticmethod
    def _error_line(cfg: Any, exc: Exception) -> str:
        kind = _classify_error(exc).value
        return VoiceChannel._error_line_for_kind(cfg, kind)

    @staticmethod
    def _error_line_for_kind(cfg: Any, kind: str) -> str:
        lines = getattr(cfg, "voice_errors", {}) or {}
        return lines.get(kind) or _DEFAULT_ERROR_LINES.get(kind, "")

    async def emit_error_line(
        self, kind: str, context: dict, agent_cfg: Any,
    ) -> bool:
        """Emit a persona-voice error line via the per-request sink.

        Called by Agent.handle_message's error branch. Returns True if
        the error was delivered to the client (caller should suppress
        normal text delivery). Returns False if no sink is present
        (e.g. this was called outside a live SSE/WS request).
        """
        sink = context.get("_error_sink")
        if sink is None:
            return False
        line = VoiceChannel._error_line_for_kind(agent_cfg, kind)
        adapter = TagDialectAdapter(agent_cfg.tts.tag_dialect)
        spoken = adapter.render(line) if line else ""
        await sink(kind, spoken)
        return True

    # --- WS ------------------------------------------------------------

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if not self._verify(request, b""):
            return web.json_response({"error": "invalid signature"}, status=401)

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # Per-utterance task map so `cancel` frames can target them.
        tasks: dict[str, asyncio.Task] = {}

        async for msg in ws:
            if msg.type.name != "TEXT":
                continue
            try:
                frame = json.loads(msg.data)
            except Exception:
                continue
            t = frame.get("type")

            if t == "stt_start":
                scope_id = frame.get("scope_id") or "anon"
                self.pool.schedule_prewarm(
                    scope_id, lambda s=scope_id: self._prewarm(s),
                )
                continue

            if t == "stage":
                # Hints only — no-op in 2.3.
                continue

            if t == "cancel":
                uid = frame.get("utterance_id")
                task = tasks.get(uid) if uid else None
                if task is not None and not task.done():
                    task.cancel()
                continue

            if t == "utterance":
                uid = frame.get("utterance_id") or str(uuid.uuid4())
                tasks[uid] = asyncio.create_task(
                    self._run_ws_utterance(ws, frame, uid),
                )
                continue

        # Clean up dangling tasks on WS close.
        for task in tasks.values():
            if not task.done():
                task.cancel()
        return ws

    async def _run_ws_utterance(
        self, ws: web.WebSocketResponse, frame: dict, uid: str,
    ) -> None:
        agent_role = frame.get("agent_role", self.default_agent)
        cfg = self._agent_configs.get(agent_role)
        if cfg is None:
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": "unknown", "spoken": "",
            })
            return

        scope_id = self._resolve_scope_id({
            "scope_id": frame.get("scope_id"),
            "context": frame.get("context") or {},
        })
        self.pool.ensure(scope_id)
        self.pool.touch(scope_id)

        splitter = ProsodicSplitter()
        adapter = TagDialectAdapter(cfg.tts.tag_dialect)
        last_text = ""
        error_emitted = False

        async def on_token(accumulated: str) -> None:
            nonlocal last_text
            delta = accumulated[len(last_text):]
            last_text = accumulated
            for block in splitter.feed(delta):
                await ws.send_json({
                    "type": "block", "utterance_id": uid,
                    "text": adapter.render(block), "final": False,
                })

        async def _error_sink(kind: str, spoken: str) -> None:
            nonlocal error_emitted
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": kind, "spoken": spoken,
            })
            error_emitted = True

        bus_msg = BusMessage(
            type=MessageType.REQUEST, source="voice", target=agent_role,
            content=frame.get("text", ""),
            channel="voice",
            context={
                "chat_id": scope_id, "utterance_id": uid,
                **(frame.get("context") or {}),
                "_on_token": on_token,
                "_error_sink": _error_sink,
            },
        )

        try:
            await self._bus.request(bus_msg, timeout=300)
            if error_emitted:
                return
            tail = splitter.flush_tail()
            if tail:
                await ws.send_json({
                    "type": "block", "utterance_id": uid,
                    "text": adapter.render(tail), "final": True,
                })
            await ws.send_json({"type": "done", "utterance_id": uid})
        except asyncio.CancelledError:
            # Cancellation from a `cancel` frame — drop partial state; do
            # not emit `done`. Pool stays alive per spec §10.3.
            raise
        except Exception as exc:
            line = self._error_line(cfg, exc)
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": _classify_error(exc).value,
                "spoken": adapter.render(line) if line else "",
            })

    async def _prewarm(self, scope_id: str) -> None:
        session_id = f"voice:{scope_id}:{self.default_agent}"
        try:
            await self._memory.ensure_session(
                session_id=session_id,
                agent_role=self.default_agent,
                user_peer="voice_speaker",
            )
            cfg = self._agent_configs.get(self.default_agent)
            tokens = getattr(
                getattr(cfg, "memory", None), "token_budget", 800,
            )
            await self._memory.get_context(
                session_id=session_id,
                agent_role=self.default_agent,
                tokens=tokens,
                search_query=None,
                user_peer="voice_speaker",
            )
        except Exception as exc:
            logger.warning("Voice prewarm failed for %s: %s", scope_id, exc)


async def _write_sse(response: web.StreamResponse, event: str, data: dict) -> None:
    payload = (
        f"event: {event}\n"
        f"data: {json.dumps(data)}\n\n"
    )
    await response.write(payload.encode("utf-8"))
