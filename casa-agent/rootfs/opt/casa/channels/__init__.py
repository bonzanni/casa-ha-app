"""Channel abstraction for Casa agent I/O."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Channel(ABC):
    """Base class for all communication channels."""

    name: str
    default_agent: str

    @abstractmethod
    async def start(self) -> None:
        """Start listening for incoming messages."""

    @abstractmethod
    async def send(self, message: str, context: dict) -> None:
        """Send a message through the channel."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""


class ChannelManager:
    """Registry and lifecycle manager for channels."""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> None:
        """Register a channel by its name."""
        self._channels[channel.name] = channel

    def get(self, name: str) -> Channel | None:
        """Return the channel with *name*, or ``None``."""
        return self._channels.get(name)

    async def start_all(self) -> None:
        """Start all registered channels."""
        for ch in self._channels.values():
            await ch.start()

    async def stop_all(self) -> None:
        """Stop all registered channels, swallowing errors."""
        for ch in self._channels.values():
            try:
                await ch.stop()
            except Exception:
                logger.exception("Error stopping channel %s", ch.name)
