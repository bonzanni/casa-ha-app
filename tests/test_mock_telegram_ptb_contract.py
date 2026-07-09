"""Mock-drift guard: the mock Telegram server's payloads must parse under the
REAL python-telegram-bot de_json.

Both 2026-07 E-block breakages were the mock lying about a dependency: its
`getChatMember` payload was too thin for PTB 22.7's strict `ChatMember`/`User`
parsing, so the engagement e2e failed only in tier2 — after six red releases.
This pins the mock's payload builders to PTB's actual parsers in the fast unit
gate, so a future PTB bump (or a mock edit) fails fast with a precise error.

Runs the parse in a SUBPROCESS on purpose: tests/conftest.py installs a fake
`telegram` stub into sys.modules for the whole unit session (so most tests
need no real PTB), which would shadow the real parsers here. A fresh
interpreter sees the real python-telegram-bot (installed via requirements) and
leaks no module state back into the stubbed session.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
MOCK_DIR = REPO / "test-local" / "e2e" / "mock_telegram"

_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {mock_dir!r})
    import server as mock
    from telegram import (
        ChatMember, ChatMemberAdministrator, ForumTopic, Message, User,
    )

    BOT = 4242

    u = User.de_json(mock.build_getme_result(BOT), bot=None)
    assert isinstance(u, User) and u.id == BOT and u.is_bot and u.first_name, "getMe"

    for cmt in (True, False):
        m = ChatMember.de_json(mock.build_getchatmember_result(BOT, cmt), bot=None)
        assert isinstance(m, ChatMemberAdministrator), "getChatMember type"
        assert m.can_manage_topics is cmt, "can_manage_topics"
        assert m.user.id == BOT and m.user.is_bot, "getChatMember user"

    for thread in (None, 1001):
        data = mock.build_sendmessage_result(7, chat_id=-1001, text="hi", thread_id=thread)
        msg = Message.de_json(data, bot=None)
        assert isinstance(msg, Message) and msg.message_id == 7 and msg.chat is not None, "sendMessage"
        if thread:
            assert msg.message_thread_id == thread, "sendMessage thread"

    ft = ForumTopic.de_json(mock.build_createforumtopic_result(1001, "probe topic"), bot=None)
    assert isinstance(ft, ForumTopic) and ft.message_thread_id == 1001 and ft.name == "probe topic", "createForumTopic"

    print("OK")
    """
).format(mock_dir=str(MOCK_DIR))


def test_mock_payloads_parse_under_real_ptb() -> None:
    r = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True,
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (
        "mock Telegram payloads no longer parse under real python-telegram-bot "
        "(mock drift or a PTB bump):\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
