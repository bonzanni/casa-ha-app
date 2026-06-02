"""Unit tests for the session sweeper (spec 5.2 §6)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from session_registry import SessionRegistry
from session_sweeper import SessionSweeper


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """Format a UTC datetime to the same ISO string SessionRegistry writes."""
    return dt.isoformat()


async def _seed(reg: SessionRegistry, key: str, sdk_sid: str, last_active: datetime) -> None:
    """Seed an entry with an explicit last_active (bypasses register's 'now')."""
    async with reg._lock:
        reg._data[key] = {
            "agent": "assistant",
            "sdk_session_id": sdk_sid,
            "last_active": _iso(last_active),
        }
        await reg._save_locked()


# ---------------------------------------------------------------------------
# Pure eviction policy — TTL boundaries, channel classification
# ---------------------------------------------------------------------------


class TestEvictionPolicy:
    async def test_active_entries_survive_sweep(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        # 5 active tg entries (10 days old — well under 30-day TTL).
        for i in range(5):
            await _seed(
                reg, f"telegram-{i}", f"sdk-{i}",
                last_active=now - timedelta(days=10),
            )

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert len(reg.all_entries()) == 5

    async def test_expired_standard_entries_are_evicted(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        # 3 active + 2 expired (31 days old).
        for i in range(3):
            await _seed(reg, f"telegram-{i}", f"sdk-{i}", now - timedelta(days=10))
        for i in range(3, 5):
            await _seed(reg, f"telegram-{i}", f"sdk-{i}", now - timedelta(days=31))

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        remaining = reg.all_entries()
        assert set(remaining.keys()) == {"telegram-0", "telegram-1", "telegram-2"}

        # Disk state agrees.
        on_disk = json.loads((tmp_path / "sessions.json").read_text())
        assert set(on_disk.keys()) == {"telegram-0", "telegram-1", "telegram-2"}

    async def test_ttl_boundary_is_inclusive_on_keep_side(self, tmp_path):
        """An entry whose age equals the TTL exactly is KEPT (not evicted).

        Spec §6.2 says "older than SESSION_TTL_DAYS". Exactly equal is not older.
        """
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        await _seed(reg, "telegram-x", "sdk-x", now - timedelta(days=30))

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert reg.get("telegram-x") is not None

    async def test_webhook_uuid_scope_uses_short_ttl(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        one_shot = str(uuid.uuid4())
        # 2 days old: under the 30-day standard TTL, OVER the 1-day webhook TTL.
        await _seed(
            reg, f"webhook-{one_shot}", "sdk-uuid",
            now - timedelta(days=2),
        )

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert reg.get(f"webhook-{one_shot}") is None

    async def test_sweep_extracts_channel_from_hyphen_key(self, tmp_path):
        """Post v0.17.1 the registry key shape is {channel}-{scope_id}; the
        sweeper must partition on '-' to read the channel correctly when
        classifying webhook-vs-session TTL.
        """
        # Fabricate a registry with one expired webhook UUID-scope entry
        # written under the new hyphen shape.
        old_iso = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
        path = tmp_path / "sessions.json"
        path.write_text(json.dumps({
            "webhook-12345678-1234-1234-1234-123456789012": {
                "agent": "assistant",
                "sdk_session_id": "sdk-x",
                "last_active": old_iso,
            },
        }))
        reg = SessionRegistry(str(path))
        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,  # < 2 days elapsed
        )
        await sweeper._sweep_once()
        assert reg.get(
            "webhook-12345678-1234-1234-1234-123456789012"
        ) is None  # evicted under webhook TTL

    async def test_webhook_non_uuid_scope_uses_standard_ttl(self, tmp_path):
        """A webhook entry with a deliberately-pinned non-UUID chat_id is NOT
        treated as a one-shot. It gets the standard TTL like any other channel.
        """
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        # 2 days old: under standard 30-day TTL → survives.
        await _seed(
            reg, "webhook-ha-automation-daily", "sdk-pinned",
            now - timedelta(days=2),
        )

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert reg.get("webhook-ha-automation-daily") is not None

    async def test_non_webhook_channels_ignore_webhook_ttl(self, tmp_path):
        """A 2-day-old telegram entry whose scope_id happens to be a UUID must
        NOT be evicted — the short TTL is webhook-only.
        """
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        coincidental_uuid = str(uuid.uuid4())
        await _seed(
            reg, f"telegram-{coincidental_uuid}", "sdk-tg",
            now - timedelta(days=2),
        )

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert reg.get(f"telegram-{coincidental_uuid}") is not None

    async def test_unparseable_last_active_is_evicted(self, tmp_path):
        """A corrupt / missing last_active is treated as stale garbage."""
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        async with reg._lock:
            reg._data["telegram-bad"] = {
                "agent": "assistant",
                "sdk_session_id": "sdk-bad",
                "last_active": "not-a-date",
            }
            reg._data["telegram-missing"] = {
                "agent": "assistant",
                "sdk_session_id": "sdk-missing",
                # no last_active field
            }
            await reg._save_locked()

        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert reg.get("telegram-bad") is None
        assert reg.get("telegram-missing") is None

    async def test_no_evictions_triggers_no_save(self, tmp_path, monkeypatch):
        """If nothing needs eviction, the sweep must not rewrite the file."""
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        await _seed(reg, "telegram-1", "sdk-1", now - timedelta(days=1))

        save_calls = [0]
        orig = reg._save_locked

        async def counting_save_locked():
            save_calls[0] += 1
            await orig()

        monkeypatch.setattr(reg, "_save_locked", counting_save_locked)

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert save_calls[0] == 0, \
            "No evictions → no save — avoid needless disk write every 6 h"

    async def test_evict_logs_one_info_with_count(self, tmp_path, caplog):
        """The sweep emits ONE info line per pass when evictions occur,
        including the count. Avoids one-log-per-entry log spam.
        """
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        for i in range(7):
            await _seed(reg, f"telegram-{i}", f"sdk-{i}", now - timedelta(days=60))

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        caplog.set_level(logging.INFO, logger="session_sweeper")
        await sweeper._sweep_once()

        evict_lines = [
            r for r in caplog.records
            if r.name == "session_sweeper" and r.levelno == logging.INFO
            and "evicted" in r.message.lower()
        ]
        assert len(evict_lines) == 1
        assert "7" in evict_lines[0].message


# ---------------------------------------------------------------------------
# Concurrency — sweep + register interleaved
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_register_during_sweep_does_not_tear(self, tmp_path):
        """A sweep in flight must not lose a concurrent register()."""
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        # 5 expired entries to evict.
        for i in range(5):
            await _seed(reg, f"telegram-old-{i}", f"sdk-{i}", now - timedelta(days=60))

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        # Fire sweep + register concurrently on the same event loop.
        await asyncio.gather(
            sweeper._sweep_once(),
            reg.register("telegram-new", "assistant", "sdk-new"),
        )

        remaining = reg.all_entries()
        # All 5 old entries gone, new entry present.
        assert set(remaining.keys()) == {"telegram-new"}

        on_disk = json.loads((tmp_path / "sessions.json").read_text())
        assert set(on_disk.keys()) == {"telegram-new"}

    async def test_sweep_holds_lock_during_eviction(self, tmp_path):
        """Register() called during the critical section must wait for it."""
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        for i in range(3):
            await _seed(reg, f"telegram-old-{i}", f"sdk-{i}", now - timedelta(days=60))

        # Block inside the sweep by wrapping _save_locked with a release-timed
        # suspension. While the sweep holds the lock, a concurrent register()
        # must still be waiting.
        release = asyncio.Event()
        orig_save = reg._save_locked

        async def slow_save_locked():
            await release.wait()
            await orig_save()

        reg._save_locked = slow_save_locked  # type: ignore[method-assign]

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )

        sweep_task = asyncio.create_task(sweeper._sweep_once())
        # Let the sweep acquire the lock.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        register_task = asyncio.create_task(
            reg.register("telegram-new", "assistant", "sdk-new"),
        )
        await asyncio.sleep(0.02)
        assert not register_task.done(), \
            "register() must block while sweep holds the registry lock"

        release.set()
        await asyncio.gather(sweep_task, register_task)
        assert reg.get("telegram-new") is not None
        for i in range(3):
            assert reg.get(f"telegram-old-{i}") is None


# ---------------------------------------------------------------------------
# SDK session prune seam — forward-compat, no-op today
# ---------------------------------------------------------------------------


class TestSdkSessionPrune:
    async def test_prune_called_once_per_eviction_when_sdk_exposes_method(
        self, tmp_path, monkeypatch,
    ):
        """_sdk_delete_session (the test seam) is called once per evicted
        sdk_session_id with the right session id. Eviction is the source of
        truth regardless of how the underlying SDK is invoked.

        (Previously used AsyncMock on claude_agent_sdk.delete_session directly;
        updated to the new contract where _sdk_delete_session is the test seam
        and the call is dispatched via asyncio.to_thread, §3.4.1.)
        """
        import session_sweeper

        calls = []

        def fake_delete(session_id, directory=None):
            calls.append(session_id)

        monkeypatch.setattr(session_sweeper, "_sdk_delete_session", fake_delete)

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        await _seed(reg, "telegram-1", "sdk-1", now - timedelta(days=60))
        await _seed(reg, "telegram-2", "sdk-2", now - timedelta(days=60))

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        # Both entries must be evicted from the registry (the invariant that matters).
        assert reg.all_entries() == {}
        # And the delete seam is called once per evicted session id.
        assert sorted(calls) == ["sdk-1", "sdk-2"]

    async def test_prune_missing_method_is_silent_noop(
        self, tmp_path, monkeypatch,
    ):
        """If the lazy import or delete call inside _sdk_delete_session raises
        (e.g. the SDK does not expose delete_session), the reap is silently
        swallowed and eviction still happens — no errors, no warnings surfaced.
        """
        import claude_agent_sdk

        if hasattr(claude_agent_sdk, "delete_session"):
            monkeypatch.delattr(claude_agent_sdk, "delete_session")

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        await _seed(reg, "telegram-1", "sdk-1", now - timedelta(days=60))

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()

        assert reg.get("telegram-1") is None  # eviction still happened

    async def test_prune_raising_does_not_break_sweep(
        self, tmp_path, monkeypatch,
    ):
        """A buggy SDK-side delete must not stop the sweep mid-pass or
        re-surface the entry in the registry.

        Patches the _sdk_delete_session seam with a sync function that raises,
        exercising the except-Exception path in _reap_transcript. Eviction must
        still happen — the reap failure is best-effort and swallowed silently.
        """
        import session_sweeper

        def boom(session_id, directory=None):
            raise RuntimeError(f"SDK rejected {session_id}")

        monkeypatch.setattr(session_sweeper, "_sdk_delete_session", boom)

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        await _seed(reg, "telegram-1", "sdk-1", now - timedelta(days=60))
        await _seed(reg, "telegram-2", "sdk-2", now - timedelta(days=60))

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        await sweeper._sweep_once()  # must not raise

        # Both entries evicted despite the SDK-prune raising.
        assert reg.all_entries() == {}


# ---------------------------------------------------------------------------
# Lifecycle — start/stop background task
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_spawns_background_task_that_runs_periodic_sweeps(
        self, tmp_path,
    ):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = [datetime(2026, 4, 18, tzinfo=timezone.utc)]

        # 1 expired entry we can watch get evicted by the periodic tick.
        await _seed(reg, "telegram-old", "sdk-old", now[0] - timedelta(days=60))

        # Use a very short sweep interval so the test completes quickly.
        # sweep_interval_hours is converted to seconds internally; pass a
        # fractional hour corresponding to ~20 ms.
        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=0.02 / 3600,  # ≈ 20 ms
            now=lambda: now[0],
        )
        sweeper.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if reg.get("telegram-old") is None:
                    break
            assert reg.get("telegram-old") is None
        finally:
            await sweeper.stop()

    async def test_stop_before_start_is_safe(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
        )
        await sweeper.stop()  # no-op, must not raise

    async def test_double_start_is_safe(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
        )
        sweeper.start()
        sweeper.start()  # idempotent — must not spawn a second task
        try:
            assert sweeper._task is not None
        finally:
            await sweeper.stop()

    async def test_stop_cancels_task_cleanly(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
        )
        sweeper.start()
        await asyncio.sleep(0)
        await sweeper.stop()  # must not raise, must not hang
        assert sweeper._task is None


# ---------------------------------------------------------------------------
# Transcript reaper — _reap_transcript / _sdk_delete_session
# ---------------------------------------------------------------------------


class TestTranscriptReaper:
    async def test_reaper_calls_delete_session_with_directory(self, monkeypatch):
        import session_sweeper

        calls = []

        def fake_delete(session_id, directory=None):
            calls.append((session_id, directory))

        monkeypatch.setattr(session_sweeper, "_sdk_delete_session", fake_delete, raising=False)
        await session_sweeper._reap_transcript("sid-7", "/addon_configs/casa-agent/agent-home/assistant")
        assert calls == [("sid-7", "/addon_configs/casa-agent/agent-home/assistant")]

    async def test_freshness_guard_keeps_recent_voice_entry_alive(self, tmp_path):
        """A voice entry that is inside its freshness window must NOT be evicted
        even if the nominal TTL has been exceeded (spec §3.4(3)).

        voice freshness_window defaults to 30 minutes. We construct a sweeper
        with a tiny TTL (1 second) and a 'now' that is 10 seconds past last_active
        — well past the ttl but inside the 30-minute freshness window.
        guard = max(ttl, freshness_window) == freshness_window → entry survives.
        """
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)

        base_time = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
        last_active = base_time
        # 'now' is 10 seconds later — past 1-second ttl, but inside 30-min freshness
        now = base_time + timedelta(seconds=10)

        async with reg._lock:
            reg._data["voice-user-123"] = {
                "agent": "assistant",
                "sdk_session_id": "sdk-voice-fresh",
                "last_active": last_active.isoformat(),
            }
            await reg._save_locked()

        sweeper = SessionSweeper(
            registry=reg,
            # ttl of 1 second — entry is past this
            session_ttl_days=0,  # will produce timedelta(0), guard will use freshness
            webhook_session_ttl_days=0,
            sweep_interval_hours=6,
            now=lambda: now,
        )
        # Patch session_ttl to a tiny value so we can test below freshness_window
        sweeper._session_ttl = timedelta(seconds=1)
        sweeper._webhook_ttl = timedelta(seconds=1)

        await sweeper._sweep_once()

        # The voice entry must survive because freshness_window("voice") == 30 min
        assert reg.get("voice-user-123") is not None, (
            "Voice entry inside its freshness window must not be evicted"
        )

    async def test_sweep_threads_per_role_directory_into_reaper(
        self, tmp_path, monkeypatch,
    ):
        """The sweep must pass each evicted entry's role directory to the reaper.

        Seeds two cold entries with distinct agent roles, constructs the sweeper
        with a directory_for lambda, and asserts the recorded (session_id, directory)
        pairs match each entry's role.
        """
        import session_sweeper

        recorded: list[tuple[str, str | None]] = []

        def fake_delete(session_id, directory=None):
            recorded.append((session_id, directory))

        monkeypatch.setattr(session_sweeper, "_sdk_delete_session", fake_delete)

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        old = now - timedelta(days=60)

        # Seed two entries with distinct roles.
        async with reg._lock:
            reg._data["telegram-sdk-a"] = {
                "agent": "assistant",
                "sdk_session_id": "sdk-a",
                "last_active": old.isoformat(),
            }
            reg._data["telegram-sdk-b"] = {
                "agent": "butler",
                "sdk_session_id": "sdk-b",
                "last_active": old.isoformat(),
            }
            await reg._save_locked()

        sweeper = SessionSweeper(
            registry=reg,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
            sweep_interval_hours=6,
            now=lambda: now,
            directory_for=lambda role: f"/home/{role}",
        )
        await sweeper._sweep_once()

        # Both entries must be evicted.
        assert reg.all_entries() == {}

        # Reaper must have received the per-role directory for each session.
        assert sorted(recorded) == [
            ("sdk-a", "/home/assistant"),
            ("sdk-b", "/home/butler"),
        ]
