# tests/test_tier_classifier.py
"""Unit tests for the production per-item tier classifier (mocked SDK)."""
from __future__ import annotations

import sys
import types

import pytest

import tier_classifier
from sensitivity import DEFAULT_TIER

pytestmark = [pytest.mark.unit]


class _FakeText:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAssistant:
    def __init__(self, text: str) -> None:
        self.content = [_FakeText(text)]


def _install_fake_sdk(
    monkeypatch, *, reply: str | None = None, raise_exc: Exception | None = None,
    capture: dict | None = None,
):
    """Install a fake claude_agent_sdk module whose query() yields one AssistantMessage.
    If ``capture`` is given, the kwargs passed to ClaudeAgentOptions are recorded into it."""
    fake = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:  # noqa: N801 — mirrors SDK name
        def __init__(self, **kw):
            self.kw = kw
            if capture is not None:
                capture.update(kw)

    class AssistantMessage:  # noqa: N801
        pass

    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.AssistantMessage = _FakeAssistant if reply is not None else AssistantMessage

    async def query(*, prompt, options):  # noqa: ANN001
        if raise_exc is not None:
            raise raise_exc
        if reply is not None:
            yield _FakeAssistant(reply)

    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


async def test_classify_returns_parsed_tier(monkeypatch):
    _install_fake_sdk(monkeypatch, reply="private")
    assert await tier_classifier.classify_tier("Nicola's salary is 5000 EUR") == "private"


async def test_classify_defaults_private_on_unparseable(monkeypatch):
    _install_fake_sdk(monkeypatch, reply="I am not sure")
    assert await tier_classifier.classify_tier("ambiguous") == DEFAULT_TIER


async def test_classify_defaults_private_on_error(monkeypatch):
    _install_fake_sdk(monkeypatch, raise_exc=RuntimeError("sdk boom"))
    assert await tier_classifier.classify_tier("anything") == DEFAULT_TIER


async def test_classify_blank_content_is_default(monkeypatch):
    _install_fake_sdk(monkeypatch, reply="public")
    assert await tier_classifier.classify_tier("   ") == DEFAULT_TIER


async def test_classify_uses_root_safe_permission_mode(monkeypatch):
    """The classifier must NOT use ``bypassPermissions``: the SDK turns that into
    ``--dangerously-skip-permissions``, which the bundled ``claude`` CLI refuses to
    run as root — and HA add-ons run as root, so it would fail and silently default
    every item to ``private``. Regression guard for the prod incident found on
    v0.45.0 (fixed v0.45.1)."""
    captured: dict = {}
    _install_fake_sdk(monkeypatch, reply="family", capture=captured)
    await tier_classifier.classify_tier("the family dinner is at 7")
    assert captured.get("permission_mode") != "bypassPermissions"
    assert captured.get("permission_mode") == "acceptEdits"
