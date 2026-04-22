"""DriverProtocol — abstract base class for engagement drivers.

Two implementations:
- in_casa_driver.InCasaDriver (full)
- claude_code_driver.ClaudeCodeDriver (stub, Plan 5)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from engagement_registry import EngagementRecord


class DriverProtocol(ABC):
    """Lifecycle interface all engagement drivers must honour."""

    @abstractmethod
    async def start(
        self,
        engagement: EngagementRecord,
        prompt: str,
        options: Any,
    ) -> None:
        """Spin up the engaged agent. For in_casa: instantiate a
        ClaudeSDKClient with *options* and open the connection."""

    @abstractmethod
    async def send_user_turn(
        self,
        engagement: EngagementRecord,
        text: str,
    ) -> None:
        """Feed a user turn into the engaged agent and stream its reply
        out via the engagement's topic channel."""

    @abstractmethod
    async def cancel(self, engagement: EngagementRecord) -> None:
        """Tear down the underlying client. Idempotent."""

    @abstractmethod
    async def resume(
        self,
        engagement: EngagementRecord,
        session_id: str,
    ) -> None:
        """Rehydrate a suspended engagement by re-opening the client with
        ``resume=session_id``. Raises on failure; caller decides retry."""

    @abstractmethod
    def is_alive(self, engagement: EngagementRecord) -> bool:
        """Return True when the driver has a live client for this engagement."""
