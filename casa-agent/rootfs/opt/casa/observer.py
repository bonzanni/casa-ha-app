"""Engagement observer.

Subscribes to engagement.*.event bus messages via a dedicated ``observer``
target queue. Static classifier maps events to silent/peek/trigger. Trigger
events run a bounded LLM pass that may produce an interjection NOTIFICATION
onto Ellen's queue.

Plan 2 ships with hardcoded defaults; per-executor-type YAML overrides
arrive in Plan 3 when the first Tier 3 type lands.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)


# Per-engagement limit — spec §4.7.
_INTERJECTION_RATE_LIMIT = 3


Classification = Literal["silent", "peek", "trigger"]


class Observer:
    def __init__(
        self,
        *,
        bus: Any,
        engagement_registry: Any,
        model_name: str,
    ) -> None:
        self._bus = bus
        self._registry = engagement_registry
        self._model = model_name
        self._interjection_counts: dict[str, int] = {}
        self._silenced: set[str] = set()

    # -- public surface ---------------------------------------------------

    def silence(self, engagement_id: str) -> None:
        """Called by the Telegram /silent handler."""
        self._silenced.add(engagement_id)
        logger.info(
            "observer: engagement %s silenced by user", engagement_id[:8],
        )

    def is_silenced(self, engagement_id: str) -> bool:
        return engagement_id in self._silenced

    # -- bus integration --------------------------------------------------

    async def subscribe(self) -> None:
        """Register ``observer`` as a bus target. Called once at startup."""
        import asyncio  # lazy
        if not hasattr(self._bus, "queues"):
            logger.warning("observer: bus exposes no queues attribute")
            return
        self._bus.queues.setdefault("observer", asyncio.PriorityQueue())
        self._bus.handlers["observer"] = self._handle_event
        logger.info("observer: subscribed to bus target=observer")

    async def _handle_event(self, msg: Any) -> None:
        """Dispatch one engagement event."""
        content = getattr(msg, "content", {}) or {}
        event_type = content.get("event", "unknown")
        engagement_id = content.get("engagement_id", "")
        cls = self._classify(event_type, content)
        if cls == "silent":
            return
        if cls == "peek":
            logger.debug(
                "observer peek: engagement=%s event=%s",
                (engagement_id[:8] if engagement_id else "?"), event_type,
            )
            return
        # trigger
        if self.is_silenced(engagement_id):
            return
        if not self._rate_limit_ok(engagement_id):
            return
        await self._interject(engagement_id, event_type, content)
        self._count_interjection(engagement_id)

    # -- classifier -------------------------------------------------------

    def _classify(self, event_type: str, payload: dict) -> Classification:
        if event_type in ("started", "progress"):
            return "peek"
        if event_type == "user_turn":
            return "silent"
        if event_type == "tool_call":
            return "silent"  # Plan 3 adds per-type destructive_tools list
        if event_type == "tool_result":
            return "trigger" if payload.get("status") == "error" else "silent"
        if event_type in ("warn", "error", "idle_detected", "subprocess_respawn"):
            return "trigger"
        if event_type == "query_engager":
            return "trigger" if payload.get("status") == "unknown" else "silent"
        return "silent"

    # -- rate limit -------------------------------------------------------

    def _rate_limit_ok(self, engagement_id: str) -> bool:
        return self._interjection_counts.get(engagement_id, 0) < _INTERJECTION_RATE_LIMIT

    def _count_interjection(self, engagement_id: str) -> None:
        self._interjection_counts[engagement_id] = (
            self._interjection_counts.get(engagement_id, 0) + 1
        )

    # -- interjection -----------------------------------------------------

    async def _interject(
        self, engagement_id: str, event_type: str, payload: dict,
    ) -> None:
        rec = self._registry.get(engagement_id)
        if rec is None:
            return
        decision = await self._decide_interjection(event_type, payload, rec)
        if not decision.get("interject"):
            return
        text = decision.get("text", "")
        logger.info(
            "observer.interjection engagement=%s event=%s",
            engagement_id[:8], event_type,
        )
        try:
            from bus import BusMessage, MessageType  # lazy import
            await self._bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source="observer",
                target=rec.origin.get("role", "assistant"),
                content={
                    "event": "observer_interjection",
                    "engagement_id": engagement_id,
                    "triggering_event": event_type,
                    "suggested_text": text,
                },
                channel=rec.origin.get("channel", ""),
                context={
                    "cid": rec.origin.get("cid", "-"),
                    "chat_id": rec.origin.get("chat_id", ""),
                    "engagement_id": engagement_id,
                },
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("observer notify failed: %s", exc)

    async def _decide_interjection(
        self, event_type: str, payload: dict, rec: Any,
    ) -> dict:
        """Run a bounded LLM pass to decide whether to post to main chat.

        Input: event + engagement summary + last N bus events (future — in Plan 2
        we pass only event + engagement summary since bus history is not retained).
        Output: {interject: bool, text: str}.
        """
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
        )
        import sdk_logging
        system = (
            "You are Ellen's observer. Decide whether to interject in the main "
            "chat about an in-flight engagement. Respond with STRICT JSON: "
            "{\"interject\": true|false, \"text\": \"...\"}. "
            "Interject only for actionable events the user should see "
            "(errors, idle, warnings); be terse — one sentence max."
        )
        body = json.dumps({
            "event_type": event_type,
            "payload": payload,
            "engagement": {
                "id": rec.id,
                "task": rec.task,
                "role_or_type": rec.role_or_type,
            },
        })
        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=system,
            max_turns=1,
            mcp_servers={},
        )
        out = ""
        try:
            async with ClaudeSDKClient(
                sdk_logging.with_stderr_callback(
                    options, engagement_id=rec.id[:8],
                ),
            ) as client:
                await client.query(body)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for b in getattr(msg, "content", []):
                            if isinstance(b, TextBlock):
                                out += b.text
            return json.loads(out.strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "observer _decide_interjection failed: %s — suppressing", exc,
            )
            return {"interject": False, "text": ""}
