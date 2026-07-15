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
import math
import os
import uuid
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from agent import _classify_error
from bus import BusMessage, MessageBus, MessageType
from channel_authz import agent_allowed_on
from channels import Channel
from log_cid import new_cid
from provenance import sanitize_external_context
from rate_limit import RateLimiter
from channels.voice.prosodic import ProsodicSplitter
from channels.voice.session import VoiceSessionPool
from channels.voice.tts_adapter import TagDialectAdapter
from semantic_memory import SemanticMemory

logger = logging.getLogger(__name__)

_DEFAULT_ERROR_LINES = {
    "timeout":       "[flat] That took too long.",
    "rate_limit":    "[flat] I'm busy — try again shortly.",
    "sdk_error":     "[flat] I couldn't reach my brain.",
    "memory_error":  "",
    "channel_error": "[flat] Something went wrong.",
    "unknown":       "[flat] Sorry, something went wrong.",
}

# A4 (spec A4): voice turn-budget envelope. INTEGRATION_TIMEOUT_TOTAL is
# the total wall-clock time the voice transport (SSE/WS plus any fronting
# proxy) gives one turn before ITS OWN timeout would fire — a synchronous
# specialist delegation must always leave that much room. The hard cap
# holds even if INTEGRATION_TIMEOUT_TOTAL is raised later.
INTEGRATION_TIMEOUT_TOTAL: float = 30.0
_VOICE_TURN_BUDGET_HARD_CAP_S: float = 27.0
_VOICE_TURN_BUDGET_MIN_S: float = 10.0


def _voice_turn_budget_s() -> float:
    """Effective per-turn delegation budget (spec A4).

    ``min(voice_turn_budget_seconds, INTEGRATION_TIMEOUT_TOTAL - 3)``,
    configured via the ``VOICE_TURN_BUDGET_SECONDS`` env var (default 27),
    clamped to the add-on schema's ``[10, 27]`` rail regardless of
    configuration (defence in depth — HA schema-validates normal config, but
    a direct env override or schema drift must not slip a sub-10s budget
    past, which would starve every delegation).

    A non-finite configured value (``nan``/``inf``) is REJECTED and falls
    back to 27 — a NaN budget would propagate through ``min()`` and defeat
    the deadline entirely (``asyncio.wait(timeout=nan)`` never reliably
    expires), so it must fail closed here at the source.
    """
    try:
        configured = float(os.environ.get("VOICE_TURN_BUDGET_SECONDS", "27"))
    except (TypeError, ValueError):
        configured = 27.0
    if not math.isfinite(configured):
        configured = 27.0
    # Floor at the schema minimum, then apply the transport/hard-cap ceilings.
    configured = max(configured, _VOICE_TURN_BUDGET_MIN_S)
    budget = min(configured, INTEGRATION_TIMEOUT_TOTAL - 3.0)
    return min(budget, _VOICE_TURN_BUDGET_HARD_CAP_S)


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
        memory: SemanticMemory,
        idle_timeout: int,
        sse_enabled: bool = True,
        ws_enabled: bool = True,
        rate_limiter: RateLimiter | None = None,
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
        # Per-scope_id rate limit (spec 5.2 §8). None = unlimited.
        self._rate_limiter = rate_limiter

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
        # A4: capture the deadline at TRUE ingress — the first line of the
        # handler, BEFORE body-read/HMAC/JSON validation — so those (I/O-
        # bound, potentially slow) steps are counted against the 27s window
        # rather than silently extending it past HA's ~30s transport
        # timeout. Monotonic (loop.time()).
        voice_deadline = asyncio.get_running_loop().time() + _voice_turn_budget_s()

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
        # Fail-closed channel-capability gate (spec A3): unknown role and a
        # role that never declared ha_voice get the SAME 404 body — no
        # existence oracle for residents that exist but aren't voice-reachable.
        if cfg is None or not agent_allowed_on("voice", cfg):
            return web.json_response({"error": "unknown agent_role"}, status=404)

        scope_id = self._resolve_scope_id(payload)
        self.pool.ensure(scope_id, role=agent_role)
        self.pool.touch(scope_id, role=agent_role)

        # Rate limit BEFORE opening the SSE stream (spec 5.2 §8).
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check(scope_id)
            if not decision.allowed:
                logger.info(
                    "Voice SSE rate limit hit for scope_id=%s", scope_id,
                )
                response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
                await response.prepare(request)
                adapter = TagDialectAdapter(cfg.tts.tag_dialect)
                line = VoiceChannel._error_line_for_kind(cfg, "rate_limit")
                await _write_sse(response, "error", {
                    "kind": "rate_limit",
                    "spoken": adapter.render(line) if line else "",
                })
                return response

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
        # A4: write_lock serializes real SDK-streamed blocks (on_token)
        # against the synthetic progress block (_progress_sink below). The
        # flag CHECK, the wire WRITE, and the flag MUTATION all happen under
        # the SAME held lock in both closures, so there is no window where
        # the progress sink can observe speech_block_sent=False, queue
        # behind an in-flight on_token write, and then emit progress AFTER
        # real speech. speech_block_sent flips ONLY after a real block is
        # actually written (not on any token/last_text update); progress_sent
        # flips ONLY after the progress block is actually written.
        write_lock = asyncio.Lock()
        speech_block_sent = False
        progress_sent = False

        async def on_token(accumulated: str) -> None:
            nonlocal last_text, splitter, speech_block_sent
            if not accumulated.startswith(last_text):
                # AR-B (2026-07-11 design §2 point 3): a mid-turn SDK retry
                # or a divergent canonical correction breaks the
                # "accumulated always extends last_text" assumption the
                # delta slice below depends on. Reset to a fresh splitter
                # so the new cumulative re-renders cleanly from its own
                # start instead of computing a bogus delta (mid-word
                # garbage, or a silently swallowed restart).
                logger.debug(
                    "voice sse on_token non-prefix cumulative "
                    "(len=%d vs last_text len=%d); resetting splitter "
                    "scope_id=%s",
                    len(accumulated), len(last_text), scope_id,
                )
                last_text = ""
                splitter = ProsodicSplitter()
            delta = accumulated[len(last_text):]
            last_text = accumulated
            for block in splitter.feed(delta):
                async with write_lock:
                    await _write_sse(response, "block", {
                        "text": adapter.render(block),
                        "final": False,
                    })
                    speech_block_sent = True

        async def _progress_sink(text: str) -> None:
            # A4: deterministic "still working" block for a mid-turn
            # specialist delegation. Exactly once per outer voice turn
            # (progress_sent) and suppressed once the turn has spoken any
            # REAL content (speech_block_sent). The check + write + mutation
            # are ALL under the lock — see the write_lock comment above.
            # Writes a real wire `block` — NOT via on_token, whose
            # cumulative-prefix `last_text` bookkeeping would corrupt on a
            # manually-injected block not part of the accumulated SDK text.
            nonlocal progress_sent
            async with write_lock:
                if progress_sent or speech_block_sent:
                    return
                await _write_sse(response, "block", {
                    "text": adapter.render(text), "final": False,
                })
                progress_sent = True

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
                # Sanitize-and-preserve (A:§3.5): payload["context"] is
                # caller-supplied (the SSE POST body) — strip Casa-reserved
                # provenance keys before Casa's own keys are merged in below.
                **sanitize_external_context(payload.get("context")),
                "chat_id": scope_id,
                "utterance_id": utterance_id,
                "cid": request["cid"],
                "_on_token": on_token,
                "_error_sink": _error_sink,
                "_voice_deadline": voice_deadline,
                "_progress_sink": _progress_sink,
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
                # A2: stt_start carries no agent_role, so it cannot safely
                # ensure() a role-scoped pool entry (guessing self.default_agent
                # would silently mis-key a non-default resident's session).
                # Pool registration now happens lazily on the utterance frame,
                # which DOES carry agent_role. A future integration change
                # that threads agent_role onto stt_start could re-enable a
                # role-scoped prewarm here (see VoiceSessionPool.schedule_prewarm).
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
                # A4: capture the deadline at TRUE ingress — the moment the
                # utterance frame is RECEIVED here, not inside the separately-
                # scheduled _run_ws_utterance task (which may not run for an
                # unbounded interval under load). Monotonic (loop.time()).
                voice_deadline = (
                    asyncio.get_running_loop().time() + _voice_turn_budget_s()
                )
                task = asyncio.create_task(
                    self._run_ws_utterance(ws, frame, uid, voice_deadline),
                )
                tasks[uid] = task

                def _reap(done_task: asyncio.Task, uid: str = uid) -> None:
                    # Prune only if this entry wasn't overwritten by a
                    # duplicate uid. `done_task` (the callback's own arg)
                    # IS the finished task, so this closure never holds a
                    # separate strong reference to it beyond the callback's
                    # own (transient) invocation.
                    if tasks.get(uid) is done_task:
                        tasks.pop(uid, None)
                    if done_task.cancelled():
                        return
                    exc = done_task.exception()  # retrieve so GC never logs 'never retrieved'
                    if exc is not None:
                        logger.warning(
                            "Voice WS utterance task failed (utterance_id=%s): %s",
                            uid, exc,
                        )

                task.add_done_callback(_reap)
                # Drop the frame-local reference so a finished task is not
                # kept alive by this coroutine's own suspended stack frame
                # while it awaits the next WS frame.
                del task
                continue

        # Clean up dangling tasks on WS close.
        for task in list(tasks.values()):
            if not task.done():
                task.cancel()
        return ws

    async def _run_ws_utterance(
        self, ws: web.WebSocketResponse, frame: dict, uid: str,
        voice_deadline: float,
    ) -> None:
        # A4: `voice_deadline` (monotonic loop.time()) is captured by the
        # caller at utterance-frame RECEIPT — see _ws_handler — so any delay
        # between receipt and this task actually running is counted against
        # the budget rather than silently extending it.
        agent_role = frame.get("agent_role", self.default_agent)
        cfg = self._agent_configs.get(agent_role)
        # Fail-closed channel-capability gate (spec A3): a 404 can't follow
        # the WS upgrade, so an unknown role AND a role that never declared
        # ha_voice both get the same `unknown_agent` error frame, emitted
        # BEFORE any bus dispatch.
        if cfg is None or not agent_allowed_on("voice", cfg):
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": "unknown_agent", "spoken": "",
            })
            return

        scope_id = self._resolve_scope_id({
            "scope_id": frame.get("scope_id"),
            "context": frame.get("context") or {},
        })
        self.pool.ensure(scope_id, role=agent_role)
        self.pool.touch(scope_id, role=agent_role)

        # Rate limit BEFORE dispatching to the agent (spec 5.2 §8).
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check(scope_id)
            if not decision.allowed:
                logger.info(
                    "Voice WS rate limit hit for scope_id=%s utterance_id=%s",
                    scope_id, uid,
                )
                adapter = TagDialectAdapter(cfg.tts.tag_dialect)
                line = VoiceChannel._error_line_for_kind(cfg, "rate_limit")
                await ws.send_json({
                    "type": "error", "utterance_id": uid,
                    "kind": "rate_limit",
                    "spoken": adapter.render(line) if line else "",
                })
                return

        splitter = ProsodicSplitter()
        adapter = TagDialectAdapter(cfg.tts.tag_dialect)
        last_text = ""
        error_emitted = False
        # A4: mirrors the SSE handler's write_lock/speech_block_sent —
        # see its on_token for the full rationale.
        write_lock = asyncio.Lock()
        speech_block_sent = False
        progress_sent = False

        async def on_token(accumulated: str) -> None:
            nonlocal last_text, splitter, speech_block_sent
            if not accumulated.startswith(last_text):
                # AR-B — see the SSE handler's on_token for rationale.
                logger.debug(
                    "voice ws on_token non-prefix cumulative "
                    "(len=%d vs last_text len=%d); resetting splitter "
                    "utterance_id=%s scope_id=%s",
                    len(accumulated), len(last_text), uid, scope_id,
                )
                last_text = ""
                splitter = ProsodicSplitter()
            delta = accumulated[len(last_text):]
            last_text = accumulated
            for block in splitter.feed(delta):
                async with write_lock:
                    await ws.send_json({
                        "type": "block", "utterance_id": uid,
                        "text": adapter.render(block), "final": False,
                    })
                    speech_block_sent = True

        async def _progress_sink(text: str) -> None:
            # A4: see the SSE handler's _progress_sink for the full
            # exactly-once / suppress-after-real-speech rationale — the
            # check + write + mutation all happen under the held lock.
            nonlocal progress_sent
            async with write_lock:
                if progress_sent or speech_block_sent:
                    return
                await ws.send_json({
                    "type": "block", "utterance_id": uid,
                    "text": adapter.render(text), "final": False,
                })
                progress_sent = True

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
                # Sanitize-and-preserve (A:§3.5): frame["context"] is
                # caller-supplied (the WS utterance frame) — strip
                # Casa-reserved provenance keys before Casa's own keys are
                # merged in below.
                **sanitize_external_context(frame.get("context")),
                "chat_id": scope_id, "utterance_id": uid,
                "cid": new_cid(),
                "_on_token": on_token,
                "_error_sink": _error_sink,
                "_voice_deadline": voice_deadline,
                "_progress_sink": _progress_sink,
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

async def _write_sse(response: web.StreamResponse, event: str, data: dict) -> None:
    payload = (
        f"event: {event}\n"
        f"data: {json.dumps(data)}\n\n"
    )
    await response.write(payload.encode("utf-8"))
