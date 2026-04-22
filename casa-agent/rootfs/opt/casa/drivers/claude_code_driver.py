"""Stub driver. Full impl lands in Plan 5 (see parent spec §7.3)."""

from __future__ import annotations

from typing import Any

from drivers.driver_protocol import DriverProtocol
from engagement_registry import EngagementRecord


class ClaudeCodeDriver(DriverProtocol):
    async def start(
        self, engagement: EngagementRecord, prompt: str, options: Any,
    ) -> None:
        raise NotImplementedError(
            "claude_code driver — implementation deferred to Plan 5 "
            "(spec §7.3). Use driver='in_casa' for v0.11.0."
        )

    async def send_user_turn(
        self, engagement: EngagementRecord, text: str,
    ) -> None:
        raise NotImplementedError("claude_code driver — Plan 5")

    async def cancel(self, engagement: EngagementRecord) -> None:
        # No-op: no client to tear down.
        return

    async def resume(
        self, engagement: EngagementRecord, session_id: str,
    ) -> None:
        raise NotImplementedError("claude_code driver — Plan 5")

    def is_alive(self, engagement: EngagementRecord) -> bool:
        return False
