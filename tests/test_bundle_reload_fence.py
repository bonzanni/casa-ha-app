"""Task 10 checkpoint 2f: ENTRY-POINT-ONLY manual-reload fencing (spec §3.1).

Every caller that dispatches a FULL reload (the casa_reload tool + the
/admin/reload route) must acquire _PLUGIN_TOOLS_LOCK BEFORE dispatch("full"),
establishing the global lock order `_PLUGIN_TOOLS_LOCK -> reload writer/reader
lock` for every path. The fence is NEVER placed inside reload.py — that would
recreate the AB/BA deadlock against reload's own global writer/reader lock,
which a bundle transaction's dispatch("agent") already takes on the reader side
while holding _PLUGIN_TOOLS_LOCK.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_plugin_tools_lock(monkeypatch):
    """_PLUGIN_TOOLS_LOCK is a module-level asyncio.Lock() that binds to the
    first event loop it touches; pytest-asyncio runs each test in a fresh loop.
    Rebind it to a new lock on the CURRENT loop so cross-test loop reuse never
    raises 'bound to a different event loop'."""
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    yield


class _JsonReq:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def test_full_reload_is_fenced_behind_plugin_tools_lock(monkeypatch) -> None:
    """A concurrent FULL reload cannot dispatch while a bundle op holds
    _PLUGIN_TOOLS_LOCK — it is serialized behind the lock."""
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler

    log: list = []

    async def _fake_dispatch(scope, *, runtime, role=None, include_env=False):
        log.append(scope)
        return {"status": "ok", "scope": scope}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    handler = build_admin_reload_handler(runtime=object())

    await tools_mod._PLUGIN_TOOLS_LOCK.acquire()
    try:
        task = asyncio.create_task(handler(_JsonReq({"scope": "full"})))
        await asyncio.sleep(0.02)
        assert log == []                       # fenced out while the lock is held
    finally:
        tools_mod._PLUGIN_TOOLS_LOCK.release()
    await task
    assert log == ["full"]                      # ran once the lock was released


async def test_non_full_reload_is_not_fenced(monkeypatch) -> None:
    """A non-full scope must NOT be fenced (it never reloads the plugin
    snapshot) — it dispatches even while the plugin lock is held."""
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler

    log: list = []

    async def _fake_dispatch(scope, *, runtime, role=None, include_env=False):
        log.append(scope)
        return {"status": "ok", "scope": scope}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    handler = build_admin_reload_handler(runtime=object())

    async with tools_mod._PLUGIN_TOOLS_LOCK:
        await handler(_JsonReq({"scope": "agent", "role": "mtg"}))
        assert log == ["agent"]                 # ran without waiting on the lock


async def test_bundle_agent_dispatch_completes_while_holding_plugin_lock(monkeypatch) -> None:
    """Deadlock regression: a bundle op holding _PLUGIN_TOOLS_LOCK can still
    await its OWN dispatch("agent") to completion, while a concurrent FULL
    reload is fenced OUT (blocked on the plugin lock, so it never holds reload's
    writer lock while waiting) — both complete, in order, with no deadlock."""
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler

    order: list = []

    async def _fake_dispatch(scope, *, runtime, role=None, include_env=False):
        order.append(scope)
        return {"status": "ok", "scope": scope}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    handler = build_admin_reload_handler(runtime=object())

    async with tools_mod._PLUGIN_TOOLS_LOCK:
        full_task = asyncio.create_task(handler(_JsonReq({"scope": "full"})))
        await asyncio.sleep(0.02)
        assert order == []                      # full reload fenced behind the lock
        # the bundle op's own agent reload is NOT fenced — runs to completion
        await reload_mod.dispatch("agent", runtime=object(), role="mtg")
        assert order == ["agent"]

    await asyncio.wait_for(full_task, timeout=1.0)   # no deadlock
    assert order == ["agent", "full"]                # full reload ran after release
