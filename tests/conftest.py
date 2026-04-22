"""Shared test fixtures and path setup for Casa tests.

Also installs the `telegram.*` package stubs needed by the Telegram
channel tests. Installing here (once, at session start) guarantees
that every test file sees the SAME `NetworkError` / `TimedOut` /
`TelegramError` class objects, so `except NetworkError:` in
`channels.telegram` catches exceptions raised in tests via these
names. If each test file installed its own stubs instead, pytest's
alphabetical discovery order would decide which file's class "wins",
and later files' locally-defined `_FakeNetworkError` would diverge
from the one production code catches.
"""

import sys
import types
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# Ensure the Casa package root is importable.
_casa_root = str(Path(__file__).resolve().parent.parent / "casa-agent" / "rootfs" / "opt" / "casa")
if _casa_root not in sys.path:
    sys.path.insert(0, _casa_root)


# ---------------------------------------------------------------------------
# telegram.* stubs — shared canonical exception classes across all tests.
# ---------------------------------------------------------------------------


class _FakeNetworkError(Exception):
    pass


class _FakeTimedOut(Exception):
    pass


class _FakeTelegramError(Exception):
    pass


def _install_telegram_stubs() -> None:
    if "telegram" in sys.modules and getattr(
        sys.modules["telegram"], "_casa_stub", False,
    ):
        return

    tg = types.ModuleType("telegram")
    tg._casa_stub = True  # type: ignore[attr-defined]
    tg.Update = MagicMock()

    tg_const = types.ModuleType("telegram.constants")
    tg_const.ChatAction = MagicMock()
    tg.constants = tg_const

    tg_err = types.ModuleType("telegram.error")
    tg_err.TelegramError = _FakeTelegramError
    tg_err.NetworkError = _FakeNetworkError
    tg_err.TimedOut = _FakeTimedOut
    tg.error = tg_err

    tg_ext = types.ModuleType("telegram.ext")
    tg_ext.Application = MagicMock()
    tg_ext.ContextTypes = MagicMock()
    tg_ext.MessageHandler = MagicMock()
    tg_ext.filters = MagicMock()
    tg.ext = tg_ext

    # Simple shim classes so `from telegram import BotCommand, BotCommandScopeChat` works
    class _FakeBotCommand:
        def __init__(self, command, description):
            self.command = command
            self.description = description

    class _FakeBotCommandScopeChat:
        def __init__(self, chat_id):
            self.chat_id = chat_id
            self.type = "chat"

    tg.BotCommand = _FakeBotCommand
    tg.BotCommandScopeChat = _FakeBotCommandScopeChat

    sys.modules["telegram"] = tg
    sys.modules["telegram.constants"] = tg_const
    sys.modules["telegram.error"] = tg_err
    sys.modules["telegram.ext"] = tg_ext


_install_telegram_stubs()


# ---------------------------------------------------------------------------
# Forum supergroup fakes — used by telegram engagement tests.
# ---------------------------------------------------------------------------


@dataclass
class _FakeTopicState:
    thread_id: int
    name: str
    icon_emoji: str | None = None
    closed: bool = False


@dataclass
class _FakeForumSupergroup:
    chat_id: int
    topics: dict = field(default_factory=dict)
    _next_thread_id: int = 1001
    messages_by_thread: dict = field(default_factory=lambda: defaultdict(list))
    my_commands_by_scope: dict = field(default_factory=dict)
    bot_can_manage_topics: bool = True


class _FakeTelegramBot:
    def __init__(self):
        self.messages: list = []
        self._supergroups: dict = {}

    def _require_supergroup(self, chat_id):
        if chat_id not in self._supergroups:
            self._supergroups[chat_id] = _FakeForumSupergroup(chat_id=chat_id)
        return self._supergroups[chat_id]

    async def create_forum_topic(self, chat_id, name, icon_custom_emoji_id=None, **kw):
        sg = self._require_supergroup(chat_id)
        tid = sg._next_thread_id
        sg._next_thread_id += 1
        sg.topics[tid] = _FakeTopicState(thread_id=tid, name=name, icon_emoji=icon_custom_emoji_id)
        return MagicMock(message_thread_id=tid)

    async def edit_forum_topic(self, chat_id, message_thread_id, name=None, icon_custom_emoji_id=None, **kw):
        sg = self._require_supergroup(chat_id)
        topic = sg.topics[message_thread_id]
        if name is not None:
            topic.name = name
        if icon_custom_emoji_id is not None:
            topic.icon_emoji = icon_custom_emoji_id
        return True

    async def close_forum_topic(self, chat_id, message_thread_id, **kw):
        sg = self._require_supergroup(chat_id)
        sg.topics[message_thread_id].closed = True
        return True

    async def send_message(self, chat_id, text, message_thread_id=None, **kw):
        # Always register the supergroup so tests can inspect _supergroups[chat_id].
        sg = self._require_supergroup(chat_id)
        if message_thread_id is not None:
            sg.messages_by_thread[message_thread_id].append(text)
        else:
            self.messages.append((chat_id, text))
        return MagicMock(message_id=1)

    async def set_my_commands(self, commands, scope=None, **kw):
        chat_id = getattr(scope, "chat_id", None) if scope is not None else None
        if chat_id is None and self._supergroups:
            chat_id = next(iter(self._supergroups))
        if chat_id is None:
            return True
        sg = self._require_supergroup(chat_id)
        scope_key = repr(scope) if scope is not None else "default"
        sg.my_commands_by_scope[scope_key] = [
            {"command": c.command, "description": c.description} for c in commands
        ]
        return True

    async def get_chat_member(self, chat_id, user_id, **kw):
        sg = self._require_supergroup(chat_id)
        m = MagicMock()
        m.can_manage_topics = sg.bot_can_manage_topics
        return m

    async def get_me(self):
        m = MagicMock()
        m.id = 4242
        return m


@pytest.fixture
def fake_telegram_bot():
    return _FakeTelegramBot()


@pytest_asyncio.fixture
async def engagement_fixture(tmp_path):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={"role": "assistant"}, topic_id=555,
    )

    class _Fx:
        registry = reg
        active_record = rec

    return _Fx()
