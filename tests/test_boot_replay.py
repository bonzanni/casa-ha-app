"""Tests for replay_undergoing_engagements — s6 boot-replay + orphan sweep."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _make_registry(records):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path="/tmp/x-nope.json", bus=None)
    for r in records:
        reg._records[r.id] = r
    return reg


def _rec(eid, driver="claude_code", status="active"):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=eid, kind="executor", role_or_type="hello-driver",
        driver=driver, status=status, topic_id=1,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )


async def test_replay_sweeps_orphans_and_replants_missing(monkeypatch, tmp_path):
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    # Pre-existing: one legitimate UNDERGOING engagement dir, one orphan.
    (svc_root / "engagement-keep1").mkdir()
    (svc_root / "engagement-keep1" / "type").write_text("longrun\n")
    (svc_root / "engagement-orphan1").mkdir()
    (svc_root / "engagement-orphan1" / "type").write_text("longrun\n")

    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    cau_calls = []
    start_calls = []
    async def fake_cau(): cau_calls.append(1)
    async def fake_start(*, engagement_id): start_calls.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    reg = await _make_registry([_rec("keep1"), _rec("done1", status="completed")])

    driver = AsyncMock()
    driver._spawn_background_tasks = lambda rec: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver,
    )

    # Orphan gone, keep1 still there
    assert not (svc_root / "engagement-orphan1").exists()
    assert (svc_root / "engagement-keep1").exists()
    # Compile ran exactly once
    assert len(cau_calls) == 1
    # start_service called for keep1 only
    assert start_calls == ["keep1"]


async def test_replay_heals_missing_service_dir_with_known_executor(
    monkeypatch, tmp_path,
):
    """UNDERGOING engagement + missing service dir + known executor → re-plant."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    # NO service dir for keep1 — simulates manual rm-rf.
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    # Capture the planted service dir.
    write_calls: list[dict] = []
    def fake_write(**kw):
        write_calls.append(kw)
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
        return str(svc_root / f"engagement-{kw['engagement_id']}")
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    async def fake_cau():
        return None
    async def fake_start(*, engagement_id):
        return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def __init__(self, defs):
            self._defs = defs
        def get(self, t):
            return self._defs.get(t)

    exec_reg = FakeExecReg({
        "hello-driver": ExecutorDefinition(
            type="hello-driver", description="test", model="haiku",
            driver="claude_code", enabled=True,
            tools_allowed=[], tools_disallowed=[], permission_mode="bypassPermissions",
            mcp_server_names=[], idle_reminder_days=1,
            prompt_template_path="/nope/prompt.md", hooks_path=None,
            observer_policy_path=None, doctrine_dir="/nope/doctrine",
            extra_dirs=[], mirror_chat_to_topic=False,
            plugins_dir="",
        ),
    })

    reg = await _make_registry([_rec("keep1")])
    driver = AsyncMock()
    driver._spawn_background_tasks = lambda rec: None

    # M7: heal only happens when the workspace dir still exists.
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root),
    )

    # Service dir re-planted via write_service_dir.
    assert len(write_calls) == 1
    assert write_calls[0]["engagement_id"] == "keep1"
    # FIFO created in the workspace alongside the planted service.
    assert (ws_root / "keep1" / "stdin.fifo").exists()


async def test_replay_leaves_alone_unknown_executor(monkeypatch, tmp_path):
    """Missing service dir + executor type NOT in registry → log + move on."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    write_calls: list[dict] = []
    def fake_write(**kw):
        write_calls.append(kw)
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    async def noop(**_kw): return None
    async def noop2(): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", noop2)
    monkeypatch.setattr(s6_rc, "start_service", noop)

    class EmptyReg:
        def get(self, _t): return None

    reg = await _make_registry([_rec("keep1")])
    driver = AsyncMock()
    driver._spawn_background_tasks = lambda rec: None

    # Workspace present so we exercise the unknown-executor branch (not the
    # M7 missing-workspace warn-and-skip that precedes it).
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=EmptyReg(),
        engagements_root=str(ws_root),
    )

    # No re-plant.
    assert write_calls == []


async def test_replay_replants_incomplete_pair(monkeypatch, tmp_path):
    """v0.64.0: 'service dir present' no longer means 'unit present' — the
    heal predicate is pair-completeness (main + producer-for + -log sibling).
    A legacy v0.63.x dir (no producer-for, nested log/) or a torn pair is
    re-planted, which also migrates engagements surviving the upgrade."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    # Legacy layout: main dir exists, no producer-for, no -log sibling.
    legacy = svc_root / "engagement-keep1"
    legacy.mkdir()
    (legacy / "type").write_text("longrun\n")
    (legacy / "log").mkdir()  # ignored-by-compile nested dir, pre-v0.64.0
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    write_calls: list[dict] = []

    def fake_write(**kw):
        write_calls.append(kw)
        # Mirrors the real exist_ok=False semantics: collides if the old
        # dir was not removed first.
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
        return str(svc_root / f"engagement-{kw['engagement_id']}")

    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    async def fake_cau(): return None
    async def fake_start(*, engagement_id): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )

    reg = await _make_registry([_rec("keep1")])
    driver = AsyncMock()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    assert len(write_calls) == 1, "incomplete pair must be re-planted"
    assert write_calls[0]["engagement_id"] == "keep1"


async def test_replay_heals_when_only_log_sibling_survives(
    monkeypatch, tmp_path,
):
    """Torn state: -log sibling present, main dir gone (crash between the
    two rmtrees). The sweep keeps the sibling (its engagement is live);
    heal must re-plant without tripping over it — pre-fix the REAL
    write_service_dir raised FileExistsError on the sibling mkdir."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    (svc_root / "engagement-keep1-log").mkdir()
    (svc_root / "engagement-keep1-log" / "type").write_text("longrun\n")
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    async def fake_cau(): return None
    async def fake_start(*, engagement_id): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    # NOTE: write_service_dir is REAL here — the collision is the point.

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )

    reg = await _make_registry([_rec("keep1")])
    driver = AsyncMock()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    # Healed to a complete pair; no FileExistsError escaped.
    assert (svc_root / "engagement-keep1").is_dir()
    assert (svc_root / "engagement-keep1" / "producer-for").exists()
    assert (svc_root / "engagement-keep1-log" / "consumer-for").exists()


async def test_replay_one_bad_heal_does_not_abort_others(
    monkeypatch, tmp_path,
):
    """A single record's heal failure must not skip the compile/start of
    every other undergoing engagement."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    def fake_write(**kw):
        if kw["engagement_id"] == "bad1":
            raise OSError("disk full")
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
        return str(svc_root / f"engagement-{kw['engagement_id']}")

    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    cau_calls: list[int] = []
    start_calls: list[str] = []
    async def fake_cau(): cau_calls.append(1)
    async def fake_start(*, engagement_id): start_calls.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )

    reg = await _make_registry([_rec("bad1"), _rec("good1")])
    driver = AsyncMock()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"
    (ws_root / "bad1").mkdir(parents=True)
    (ws_root / "good1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    assert len(cau_calls) == 1, "compile must still run"
    assert "good1" in start_calls, "healthy engagement must still start"


async def test_replay_warn_and_skips_heal_when_workspace_missing(
    monkeypatch, tmp_path,
):
    """UNDERGOING + missing svc dir + known executor BUT missing workspace →
    warn-and-skip (M7, 4a.1 §7.3). Planting a service whose run script does
    `cd <workspace>` under set -e would crash-loop s6."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    write_calls: list[dict] = []
    monkeypatch.setattr(
        s6_rc, "write_service_dir", lambda **kw: write_calls.append(kw),
    )
    async def fake_cau(): return None
    async def fake_start(*, engagement_id): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )

    reg = await _make_registry([_rec("keep1")])
    driver = AsyncMock()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"   # exists, but keep1/ subdir deliberately absent
    ws_root.mkdir()

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    # Missing workspace → NO service planted.
    assert write_calls == []


def _rec_pa(eid, artifacts, topic_id=1):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=eid, kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=topic_id,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
        plugin_artifacts=tuple(artifacts),
    )


def _exec_reg():
    from config import ExecutorDefinition
    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True, tools_allowed=[],
                tools_disallowed=[], permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )
    return FakeExecReg()


async def test_replay_renders_plugin_dir_flags_from_record(monkeypatch, tmp_path):
    """§3.8: the run script renders --plugin-dir flags from the RECORDED
    artifacts; replay never re-resolves current assignments."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    import plugin_registry

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    write_calls: list[dict] = []
    def fake_write(**kw):
        write_calls.append(kw)
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    monkeypatch.setattr(s6_rc, "start_service", AsyncMock())
    # Replay must NEVER call resolve_for.
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda t: (_ for _ in ()).throw(
                            AssertionError("replay re-resolved!")))

    art_dir = tmp_path / "store" / "sp" / ("a" * 64)
    art_dir.mkdir(parents=True)
    rec = _rec_pa("keep1", [{"name": "superpowers", "artifact_id": "a" * 64,
                             "path": str(art_dir)}])
    reg = await _make_registry([rec])
    driver = AsyncMock(); driver._spawn_background_tasks = lambda r: None
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert len(write_calls) == 1
    assert f"--plugin-dir {art_dir}" in write_calls[0]["run_script"]


async def test_replay_refuses_when_recorded_artifact_missing(monkeypatch, tmp_path):
    """Sol F5: a missing recorded artifact refuses resume — no service written,
    no start_service, no background tasks; a topic notice fires; others heal."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    write_ids: list[str] = []
    def fake_write(**kw):
        write_ids.append(kw["engagement_id"])
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    good_dir = tmp_path / "store" / "ok" / ("b" * 64)
    good_dir.mkdir(parents=True)
    refused = _rec_pa("refused1",
                      [{"name": "gone", "artifact_id": "a" * 64,
                        "path": str(tmp_path / "does-not-exist")}], topic_id=5)
    healthy = _rec_pa("healthy1",
                      [{"name": "ok", "artifact_id": "b" * 64,
                        "path": str(good_dir)}], topic_id=6)
    reg = await _make_registry([refused, healthy])

    driver = AsyncMock()
    bg = MagicMock()
    driver._spawn_background_tasks = bg
    ws_root = tmp_path / "eng"
    (ws_root / "refused1").mkdir(parents=True)
    (ws_root / "healthy1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    # Refused: no service, no start, no background tasks; topic notice fired.
    assert "refused1" not in write_ids and "refused1" not in start_ids
    driver._send_to_topic.assert_awaited_once()
    assert driver._send_to_topic.await_args.args[0] == 5    # topic_id
    bg_ids = {c.args[0].id for c in bg.call_args_list}
    assert "refused1" not in bg_ids
    # Healthy engagement still healed + started + background-tasked.
    assert "healthy1" in write_ids and "healthy1" in start_ids
    assert "healthy1" in bg_ids
