"""Spec §4.3 channel-aware load: _plan_load(channel, is_fresh_session)."""
from __future__ import annotations
import pytest
from agent import _plan_load
pytestmark = [pytest.mark.unit]

def test_text_fresh_session_pushes_overlay_and_autorecalls():
    p = _plan_load("telegram", is_fresh_session=True)
    assert p.push_overlay is True and p.auto_recall is True

def test_voice_fresh_session_overlay_only_no_autorecall():
    p = _plan_load("voice", is_fresh_session=True)
    assert p.push_overlay is True and p.auto_recall is False

def test_resumed_session_skips_overlay_push():
    p = _plan_load("telegram", is_fresh_session=False)
    assert p.push_overlay is False and p.auto_recall is False
