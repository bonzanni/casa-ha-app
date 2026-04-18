"""Regression tests for casa_core helpers extracted from Phase 2.1 review.

These cover three runtime bugs the design review surfaced:
    - build_heartbeat_message: BusMessage must have a non-empty channel.
    - build_invoke_message: each invoke gets a unique chat_id so sessions don't
      collide on ``webhook:default``.
    - _init_heartbeat_defaults (closure variables): must be resolvable before
      the HTTP server starts, not after the scheduler block.
"""

from __future__ import annotations

import pytest

from bus import MessageType
from session_registry import build_session_key


# ---------------------------------------------------------------------------
# Heartbeat: BusMessage must have a non-empty channel
# ---------------------------------------------------------------------------


def test_build_heartbeat_message_has_scheduler_channel():
    from casa_core import build_heartbeat_message

    msg = build_heartbeat_message(agent="assistant", prompt="ping")
    assert msg.type == MessageType.SCHEDULED
    assert msg.target == "assistant"
    assert msg.channel == "scheduler"
    # agent._process resolves the session key from msg.channel + context;
    # this call must not raise.
    key = build_session_key(msg.channel, msg.context.get("chat_id"))
    assert key.startswith("scheduler:")


def test_build_heartbeat_message_preserves_prompt():
    from casa_core import build_heartbeat_message

    msg = build_heartbeat_message(agent="assistant", prompt="what's new?")
    assert msg.content == "what's new?"


# ---------------------------------------------------------------------------
# Invoke: each call gets its own session key
# ---------------------------------------------------------------------------


def test_build_invoke_message_caller_supplied_chat_id_wins():
    from casa_core import build_invoke_message

    msg = build_invoke_message(
        agent_role="assistant",
        prompt="hi",
        payload={"context": {"chat_id": "user-A"}},
    )
    assert msg.context["chat_id"] == "user-A"
    assert build_session_key(msg.channel, msg.context["chat_id"]) == "webhook:user-A"


def test_build_invoke_message_generates_chat_id_when_missing():
    from casa_core import build_invoke_message

    a = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    b = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    # Two back-to-back calls without chat_id must not collide on
    # `webhook:default` — each invocation is its own session.
    assert a.context["chat_id"] != b.context["chat_id"]
    key_a = build_session_key(a.channel, a.context["chat_id"])
    key_b = build_session_key(b.channel, b.context["chat_id"])
    assert key_a != key_b
    assert key_a != "webhook:default"


def test_build_invoke_message_target_is_agent_role():
    from casa_core import build_invoke_message

    msg = build_invoke_message(agent_role="butler", prompt="hi", payload={})
    assert msg.target == "butler"
    assert msg.channel == "webhook"
    assert msg.type == MessageType.REQUEST


# ---------------------------------------------------------------------------
# Dashboard ordering: heartbeat defaults must resolve pre-server-start
# ---------------------------------------------------------------------------


def test_init_heartbeat_defaults_is_pure_and_callable_early():
    """The helper must be side-effect-free and yield concrete values so the
    dashboard closure has bindings even before the scheduler block runs."""
    from casa_core import init_heartbeat_defaults

    enabled, interval = init_heartbeat_defaults(env={})
    assert isinstance(enabled, bool)
    assert isinstance(interval, int)
    assert interval >= 1


def test_init_heartbeat_defaults_reads_env():
    from casa_core import init_heartbeat_defaults

    enabled, interval = init_heartbeat_defaults(
        env={"HEARTBEAT_ENABLED": "false", "HEARTBEAT_INTERVAL_MINUTES": "15"}
    )
    assert enabled is False
    assert interval == 15


def test_init_heartbeat_defaults_invalid_interval_falls_back():
    """Malformed interval must not crash the dashboard."""
    from casa_core import init_heartbeat_defaults

    enabled, interval = init_heartbeat_defaults(
        env={"HEARTBEAT_INTERVAL_MINUTES": "not-a-number"}
    )
    assert interval >= 1


# ---------------------------------------------------------------------------
# Correlation id — builders attach fresh cid per message (spec 5.2 §7.2)
# ---------------------------------------------------------------------------

import re as _re


def test_build_heartbeat_message_attaches_cid():
    from casa_core import build_heartbeat_message

    msg = build_heartbeat_message(agent="assistant", prompt="ping")
    cid = msg.context.get("cid")
    assert isinstance(cid, str)
    assert _re.fullmatch(r"[0-9a-f]{8}", cid), cid


def test_build_heartbeat_message_cid_is_unique_per_call():
    from casa_core import build_heartbeat_message

    a = build_heartbeat_message(agent="assistant", prompt="ping")
    b = build_heartbeat_message(agent="assistant", prompt="ping")
    assert a.context["cid"] != b.context["cid"]


def test_build_invoke_message_attaches_cid():
    from casa_core import build_invoke_message

    msg = build_invoke_message(
        agent_role="butler", prompt="hi",
        payload={"context": {"chat_id": "user-A"}},
    )
    cid = msg.context.get("cid")
    assert isinstance(cid, str)
    assert _re.fullmatch(r"[0-9a-f]{8}", cid), cid
    # Payload-supplied fields continue to round-trip.
    assert msg.context["chat_id"] == "user-A"


def test_build_invoke_message_cid_is_unique_per_call():
    from casa_core import build_invoke_message

    a = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    b = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    assert a.context["cid"] != b.context["cid"]
