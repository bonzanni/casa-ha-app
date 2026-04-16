"""Memory provider abstraction for Casa agents."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any


class MemoryProvider(ABC):
    """Abstract base class for memory backends."""

    @abstractmethod
    async def get_context(
        self,
        peer_id: str,
        token_budget: int,
        exclude_tags: list[str] | None = None,
    ) -> str:
        """Retrieve relevant context for a peer within a token budget."""

    @abstractmethod
    async def store_message(
        self,
        session_id: str,
        peer_id: str,
        content: str,
        role: str = "user",
        tags: list[str] | None = None,
    ) -> None:
        """Store a message in the memory backend."""

    @abstractmethod
    async def create_session(self, peer_id: str) -> str:
        """Create a new session for a peer and return the session ID."""

    @abstractmethod
    async def close_session(self, session_id: str) -> None:
        """Close / finalize a session."""


class HonchoMemoryProvider(MemoryProvider):
    """Memory provider backed by the Honcho SDK.

    All SDK calls are wrapped with ``asyncio.to_thread`` so they do not
    block the event loop.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        workspace_name: str = "casa",
    ) -> None:
        # Lazy import to avoid hard failure when honcho-ai is not installed
        from honcho import Honcho  # type: ignore[import-untyped]

        self._client = Honcho(api_key=api_key, base_url=api_url)
        self._workspace_name = workspace_name
        self._workspace: Any = None

    async def initialize(self) -> None:
        """Create or retrieve the workspace."""
        self._workspace = await asyncio.to_thread(
            self._client.apps.get_or_create, name=self._workspace_name
        )

    async def get_context(
        self,
        peer_id: str,
        token_budget: int,
        exclude_tags: list[str] | None = None,
    ) -> str:
        filters: dict[str, Any] = {}
        if exclude_tags:
            filters["tags"] = {"$nin": exclude_tags}

        result = await asyncio.to_thread(
            self._client.apps.users.sessions.chat,
            app_id=self._workspace.id,
            user_id=peer_id,
            query=f"token_budget={token_budget}",
            filter=filters if filters else None,
        )
        return str(result)

    async def store_message(
        self,
        session_id: str,
        peer_id: str,
        content: str,
        role: str = "user",
        tags: list[str] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {}
        if tags:
            metadata["tags"] = tags

        await asyncio.to_thread(
            self._client.apps.users.sessions.messages.create,
            app_id=self._workspace.id,
            user_id=peer_id,
            session_id=session_id,
            content=content,
            is_user=(role == "user"),
            metadata=metadata,
        )

    async def create_session(self, peer_id: str) -> str:
        session = await asyncio.to_thread(
            self._client.apps.users.sessions.create,
            app_id=self._workspace.id,
            user_id=peer_id,
        )
        return str(session.id)

    async def close_session(self, session_id: str) -> None:
        await asyncio.to_thread(
            self._client.apps.users.sessions.delete,
            app_id=self._workspace.id,
            session_id=session_id,
        )
