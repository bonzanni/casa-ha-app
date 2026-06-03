"""Spec §4.2 #3 (next-turn-after-gap) + write_scope recording. Tests the
extracted pure helper agent._resume_decision rather than a full SDK turn."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import _resume_decision

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def _entry(sid, idle):
    return {"sdk_session_id": sid, "last_active": (NOW - idle).isoformat(), "agent": "assistant"}


def test_resume_when_fresh():
    assert _resume_decision("voice", _entry("s", timedelta(minutes=5)), NOW) == ("resume", False)


def test_new_and_save_old_when_cold():
    # voice window 30m; idle 1h → start new AND save the old session first
    assert _resume_decision("voice", _entry("s", timedelta(hours=1)), NOW) == ("new", True)


def test_new_no_save_when_no_entry():
    assert _resume_decision("voice", None, NOW) == ("new", False)


def test_new_no_save_when_entry_has_no_sid():
    assert _resume_decision("telegram", {"last_active": NOW.isoformat()}, NOW) == ("new", False)
