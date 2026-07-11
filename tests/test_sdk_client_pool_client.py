"""ManagedSdkClient unit tests (sdk_client_pool.py). Spec: docs/superpowers/
specs/2026-07-11-resident-sdk-client-pooling-design.md (AR-1..AR-10)."""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


def test_cidbox_str_and_default():
    from sdk_client_pool import _CidBox
    box = _CidBox()
    assert str(box) == "-"
    box.value = "abc123"
    assert str(box) == "abc123"


async def test_cid_filter_coerces_box_to_str():
    from log_cid import CidFilter, cid_var
    from sdk_client_pool import _CidBox
    box = _CidBox()
    box.value = "turn-2-cid"
    token = cid_var.set(box)  # type: ignore[arg-type]
    try:
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "m", (), None)
        CidFilter().filter(rec)
        assert rec.cid == "turn-2-cid"
        assert isinstance(rec.cid, str)
    finally:
        cid_var.reset(token)


# Contextvars for testing
test_origin: ContextVar = ContextVar("test_origin", default=None)
test_cid: ContextVar = ContextVar("test_cid", default="-")
test_engagement: ContextVar = ContextVar("test_engagement", default=None)


def _mk_text_block(text: str) -> _SDKTextBlock:
    """Instantiate whatever TextBlock shape the installed SDK uses."""
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    block = _mk_text_block(text)
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid: str, usage: dict[str, int] | None = None) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    if usage is not None:
        m.usage = usage  # type: ignore[attr-defined]
    return m


class ScriptedClient:
    def __init__(self, options):
        self.options = options
        self.connected = False
        self.disconnected = False
        self.interrupts = 0
        self.queries: list[str] = []
        self.script: list[list] = []      # one list of messages per query()
        self._buffer: list = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        if self.script:
            self._buffer.extend(self.script.pop(0))

    async def interrupt(self):
        self.interrupts += 1

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage
        while self._buffer:
            msg = self._buffer.pop(0)
            yield msg
            if isinstance(msg, ResultMessage):
                return


def _client(options=None, **kw):
    from sdk_client_pool import ManagedSdkClient
    return ManagedSdkClient(
        options or object(),
        origin_ctxvar=test_origin, cid_ctxvar=test_cid,
        engagement_ctxvar=test_engagement,
        make_client=ScriptedClient, **kw,
    )


async def test_open_binds_holder_and_connects():
    c = _client()
    await c.open()
    assert c.state == "warm"
    assert c._client.connected            # ScriptedClient
    # The bindings happened in open()'s context copy; we can't read them
    # from here — assert the structural pieces instead:
    assert isinstance(c.origin_holder, dict)
    assert str(c.cid_box) == "-"


async def test_open_refuses_inside_engagement():
    tok = test_engagement.set(object())
    try:
        c = _client()
        with pytest.raises(AssertionError):
            await c.open()
    finally:
        test_engagement.reset(tok)


async def test_run_turn_returns_sid_and_dispatches_messages():
    c = _client()
    await c.open()
    c._client.script = [[_mk_assistant("hello"), _mk_result("sid-1")]]
    seen = []

    async def on_message(m):
        seen.append(m)

    async with c.lock:
        sid = await c.run_turn_locked(
            "hi", origin={"cid": "c1", "channel": "voice"}, cid="c1",
            on_message=on_message,
        )
    assert sid == "sid-1"
    assert c.sid == "sid-1"
    assert c.state == "warm"
    assert len(seen) == 2
    assert c.origin_holder["channel"] == "voice"   # rewritten in place
    assert str(c.cid_box) == "c1"


async def test_second_turn_reuses_same_transport():
    c = _client()
    await c.open()
    c._client.script = [
        [_mk_assistant("a"), _mk_result("sid-1")],
        [_mk_assistant("b"), _mk_result("sid-1")],
    ]
    async def on_message(m): pass
    async with c.lock:
        await c.run_turn_locked("t1", origin={}, cid="c1", on_message=on_message)
    inner = c._client
    async with c.lock:
        await c.run_turn_locked("t2", origin={}, cid="c2", on_message=on_message)
    assert c._client is inner
    assert inner.queries == ["t1", "t2"]


async def test_origin_holder_contents_replaced_not_rebound():
    c = _client()
    await c.open()
    holder = c.origin_holder
    c._client.script = [[_mk_result("s")], [_mk_result("s")]]
    async def on_message(m): pass
    async with c.lock:
        await c.run_turn_locked("t1", origin={"cid": "one", "user_text": "a"},
                                cid="one", on_message=on_message)
    async with c.lock:
        await c.run_turn_locked("t2", origin={"cid": "two"}, cid="two",
                                on_message=on_message)
    assert c.origin_holder is holder          # same object (read-task visibility)
    assert holder == {"cid": "two"}           # cleared + rewritten, no stale keys


async def test_aclose_idempotent_and_disconnects():
    c = _client()
    await c.open()
    inner = c._client
    await c.aclose()
    await c.aclose()
    assert inner.disconnected
    assert c.state == "closed"
