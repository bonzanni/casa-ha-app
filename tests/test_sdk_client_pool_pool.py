"""SdkClientPool tests. Uses ScriptedClient + factories from
test_sdk_client_pool_client (copy the helpers — no cross-test imports)."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


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


def _mk_result(sid, usage=None, *, is_error=False, result=""):
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid
    m.is_error = is_error
    m.result = result
    if usage is not None:
        m.usage = usage
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


class FakeRegistry:
    def __init__(self):
        self.data = {}
        self.touched = []
    def get(self, key):
        return self.data.get(key)
    async def touch(self, key):
        self.touched.append(key)


def _decide_resume(channel, entry, now):
    return ("resume", False) if entry and entry.get("sdk_session_id") else ("new", False)


def _mk_pool(registry, *, decide=_decide_resume, **kw):
    from sdk_client_pool import SdkClientPool
    return SdkClientPool(
        registry, decide=decide,
        origin_ctxvar=test_origin, cid_ctxvar=test_cid,
        engagement_ctxvar=test_engagement,
        make_client=ScriptedClient, **kw,
    )


async def test_cold_hit_connects_with_resume_and_touches():
    reg = FakeRegistry()
    reg.data["voice-s1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    # Preload the script through make_client capture:
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid):
        return {"resume": resume_sid}
    async def on_message(m): pass

    async def go():
        return await pool.turn(
            channel_key="voice-s1", channel="voice", prompt="hi",
            origin={}, cid="c", build_options=build_options,
            on_stale_old=lambda s: None, on_message=on_message)

    t = asyncio.create_task(go())
    await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-0")]]
    res = await t
    assert res.resume_sid == "sid-0" and res.is_fresh is False
    assert reg.touched == ["voice-s1"]
    assert made[0].options == {"resume": "sid-0"}


async def test_warm_reuse_skips_connect_and_build_options():
    reg = FakeRegistry()
    reg.data["voice-s1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    builds = []
    async def build_options(is_fresh, resume_sid):
        builds.append((is_fresh, resume_sid))
        return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="voice-s1", channel="voice",
                               prompt="hi", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s: None,
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-0")]]
    await t
    # Turn 2 — warm: same client object, no new construction, no build_options
    made[0].script = [[_mk_result("sid-0")]]
    await go()
    assert len(made) == 1
    assert builds == [(False, "sid-0")]     # only the cold connect built options


async def test_decision_new_closes_old_awaits_disconnect_then_stale_cb():
    """AR-3 + AR-4 ordering: old entry fully disconnected BEFORE
    on_stale_old fires (cold-retain reads the flushed transcript)."""
    reg = FakeRegistry()
    reg.data["tg-1"] = {"sdk_session_id": "sid-old", "last_active": "x"}
    order = []
    def decide(channel, entry, now):
        return ("resume", False) if not order else ("new", True)
    pool = _mk_pool(reg, decide=decide)
    made = []
    def mk(opts):
        c = ScriptedClient(opts)
        real = c.disconnect
        async def d():
            order.append("disconnect")
            await real()
        c.disconnect = d
        made.append(c); return c
    pool._make_client = mk
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="tg-1", channel="telegram",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s: order.append(f"stale:{s}"),
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-old")]]
    await t
    order.append("turn2")
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[1].script = [[_mk_result("sid-new")]]
    res = await t
    assert res.is_fresh is True
    after_turn2 = order[order.index("turn2") + 1:]
    assert after_turn2[:2] == ["disconnect", "stale:sid-old"]  # AR-4 ordering


async def test_sid_mismatch_reconnects_on_registry_sid():
    reg = FakeRegistry()
    reg.data["tg-1"] = {"sdk_session_id": "sid-A", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {"resume": resume_sid}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="tg-1", channel="telegram",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s: None,
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-A")]]
    await t
    # External rewrite (another path registered a different sid):
    reg.data["tg-1"]["sdk_session_id"] = "sid-B"
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[1].script = [[_mk_result("sid-B")]]
    await t
    assert len(made) == 2
    assert made[1].options == {"resume": "sid-B"}
    assert made[0].disconnected


async def test_turn_failure_invalidates_entry_so_next_attempt_reconnects():
    # NOTE (stabilization, not an assertion change): ScriptedClient.connect()/
    # query() have no real internal awaits, so a plain `asyncio.sleep(0.01)`
    # does not reliably pause the task *between* connect completing and
    # query() being invoked — the whole cold-connect-and-turn sequence can
    # run to completion inside that sleep with nothing left to patch. Use an
    # explicit Event, set right after connect(), so the test deterministically
    # gets control back before pool.turn() calls query() on the new client.
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    connected = asyncio.Event()

    def mk(opts):
        c = ScriptedClient(opts)
        real_connect = c.connect

        async def connect():
            await real_connect()
            connected.set()
        c.connect = connect
        made.append(c)
        return c
    pool._make_client = mk
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s: None, on_message=on_message)
    t = asyncio.create_task(go())
    await connected.wait()

    class Boom(Exception): pass
    async def bad_query(prompt, session_id="default"): raise Boom()
    made[0].query = bad_query
    with pytest.raises(Boom):
        await t
    assert pool.stats()["entries"] == 0     # invalidated + dropped
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[1].script = [[_mk_result("sid-0")]]
    await t                                  # attempt 2 reconnected fine


async def test_concurrent_first_turns_single_connect():
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "sid-0", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s: None, on_message=on_message)
    t1 = asyncio.create_task(go())
    t2 = asyncio.create_task(go())
    await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-0")], [_mk_result("sid-0")]]
    await asyncio.gather(t1, t2)
    assert len(made) == 1                    # one client served both, serialized


async def test_turn_on_closing_pool_raises_poolunavailable():
    from sdk_client_pool import PoolUnavailable
    reg = FakeRegistry()
    pool = _mk_pool(reg)
    await pool.aclose()
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    with pytest.raises(PoolUnavailable):
        await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                        origin={}, cid="c", build_options=build_options,
                        on_stale_old=lambda s: None, on_message=on_message)


async def test_per_agent_cap_lru_evicts(monkeypatch):
    monkeypatch.setenv("SDK_POOL_MAX_PER_AGENT", "2")
    reg = FakeRegistry()
    for i in range(3):
        reg.data[f"v-{i}"] = {"sdk_session_id": f"sid-{i}", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go(i):
        return await pool.turn(channel_key=f"v-{i}", channel="voice",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s: None,
                               on_message=on_message)
    for i in range(3):
        t = asyncio.create_task(go(i)); await asyncio.sleep(0.01)
        made[-1].script = [[_mk_result(f"sid-{i}")]]
        await t
    assert pool.stats()["entries"] == 2
    assert made[0].disconnected          # v-0 was least-recently-used


async def test_sweeper_closes_idle_and_overage(monkeypatch):
    now = [1000.0]
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "sid-1", "last_active": "x"}
    pool = _mk_pool(reg, monotonic=lambda: now[0])
    pool.idle_seconds = 100.0
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s: None, on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("sid-1")]]
    await t
    now[0] += 50
    await pool._sweep_once()
    assert pool.stats()["entries"] == 1
    now[0] += 100                          # beyond idle bound
    await pool._sweep_once()
    assert pool.stats()["entries"] == 0
    assert made[0].disconnected


async def test_idle_bound_clamped_to_freshness(monkeypatch):
    """AR-4: idle can never exceed the channel freshness window."""
    from datetime import timedelta
    now = [0.0]
    reg = FakeRegistry()
    reg.data["voice-1"] = {"sdk_session_id": "s", "last_active": "x"}
    pool = _mk_pool(reg, monotonic=lambda: now[0])
    pool.idle_seconds = 10_000_000.0       # operator misconfig
    pool._freshness = lambda ch: timedelta(minutes=30)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="voice-1", channel="voice",
                               prompt="p", origin={}, cid="c",
                               build_options=build_options,
                               on_stale_old=lambda s: None,
                               on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("s")]]
    await t
    now[0] += 31 * 60                      # 31 min > freshness 30 min
    await pool._sweep_once()
    assert pool.stats()["entries"] == 0


async def test_fleet_cap_across_pools(monkeypatch):
    monkeypatch.setenv("SDK_POOL_FLEET_CAP", "1")
    reg = FakeRegistry()
    reg.data["a-1"] = {"sdk_session_id": "s1", "last_active": "x"}
    reg.data["b-1"] = {"sdk_session_id": "s2", "last_active": "x"}
    p1, p2 = _mk_pool(reg), _mk_pool(reg)
    made = []
    for p in (p1, p2):
        p._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go(pool, key):
        return await pool.turn(channel_key=key, channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s: None, on_message=on_message)
    t = asyncio.create_task(go(p1, "a-1")); await asyncio.sleep(0.01)
    made[-1].script = [[_mk_result("s1")]]
    await t
    t = asyncio.create_task(go(p2, "b-1")); await asyncio.sleep(0.01)
    made[-1].script = [[_mk_result("s2")]]
    await t
    assert p1.stats()["entries"] + p2.stats()["entries"] == 1
    await p1.aclose(); await p2.aclose()


async def test_evict_waits_for_entry_lock():
    """AR-7: sweep/LRU eviction must not disconnect a client whose entry
    lock is held (warm-window race: lock acquired, state still 'warm')."""
    reg = FakeRegistry()
    reg.data["v-1"] = {"sdk_session_id": "s", "last_active": "x"}
    pool = _mk_pool(reg)
    made = []
    pool._make_client = lambda opts: made.append(ScriptedClient(opts)) or made[-1]
    async def build_options(is_fresh, resume_sid): return {}
    async def on_message(m): pass
    async def go():
        return await pool.turn(channel_key="v-1", channel="voice", prompt="p",
                               origin={}, cid="c", build_options=build_options,
                               on_stale_old=lambda s: None, on_message=on_message)
    t = asyncio.create_task(go()); await asyncio.sleep(0.01)
    made[0].script = [[_mk_result("s")]]
    await t
    entry = pool._entries["v-1"]
    async with entry.lock:                      # simulate the warm window
        evict = asyncio.create_task(pool._evict("v-1", entry))
        await asyncio.sleep(0.05)
        assert not evict.done()                 # eviction is waiting, not closing
        assert not made[0].disconnected
    await evict
    assert made[0].disconnected
    assert pool.stats()["entries"] == 0
