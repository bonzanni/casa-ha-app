"""Role-scoped collision-safe session keys + v2 migration + consumers (spec A2).

asyncio_mode is ``auto`` (pytest.ini), so async tests run without an explicit
marker; the module marker is therefore ONLY ``unit`` — applying
``pytest.mark.asyncio`` module-wide would tag the synchronous tests here and
emit spurious "marked async but not an async function" warnings.
"""
from __future__ import annotations
import copy, json, re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
pytestmark = [pytest.mark.unit]


class TestScopedKey:
    def test_channel_first_so_partition_yields_channel(self):
        from session_registry import build_scoped_session_key as k
        key = k("voice", "concierge", "dev-1")
        assert key.partition("-")[0] == "voice"          # consumers rely on this
        assert "-v2-" in key

    def test_distinct_roles_distinct_keys(self):
        from session_registry import build_scoped_session_key as k
        assert k("voice", "butler", "d") != k("voice", "concierge", "d")

    def test_hyphen_tuples_cannot_collide(self):
        from session_registry import build_scoped_session_key as k
        assert k("voice", "a", "b-c") != k("voice", "a-b", "c")

    def test_charset_safe_and_bounded(self):
        from session_registry import build_scoped_session_key as k
        key = k("voice", "butler", "device-abc")
        assert re.fullmatch(r"[A-Za-z0-9_-]+", key) and len(key) <= 100

    def test_none_scope_maps_to_default(self):
        from session_registry import build_scoped_session_key as k
        assert k("voice", "butler", None) == k("voice", "butler", "default")


class TestResumeRoleCheck:
    def _entry(self, agent=None):
        e = {"sdk_session_id": "s1", "last_active": datetime.now(timezone.utc).isoformat()}
        if agent is not None:
            e["agent"] = agent
        return e

    def test_mismatched_agent_starts_new(self):
        from agent import _resume_decision
        d, _ = _resume_decision("voice", self._entry("butler"), datetime.now(timezone.utc), role="concierge")
        assert d == "new"

    def test_matching_agent_resumes(self):
        from agent import _resume_decision
        d, _ = _resume_decision("voice", self._entry("butler"), datetime.now(timezone.utc), role="butler")
        assert d == "resume"

    def test_legacy_entry_without_agent_still_starts_new_when_role_given(self):
        # Strict: migration drops agent-less voice entries, so any agent-less
        # entry seen with a role is treated as non-matching (defense in depth).
        from agent import _resume_decision
        d, _ = _resume_decision("voice", self._entry(None), datetime.now(timezone.utc), role="butler")
        assert d == "new"


class TestResumeAuthorityRoleBound:
    """IMPORTANT-2 (review): prove the role gate is wired at the TWO real
    resume-authority seams, not merely in the pure helper. Chosen approach:
    exercise BOTH real call sites directly with a mismatched-agent registry
    entry (rather than two full alternating-Agent turns) — it is deterministic,
    light, and fails precisely if EITHER seam drops ``role``:

      (a) the ``decide=`` wrapper installed in ``Agent.__init__`` (the pooled
          path's resume authority), called via ``agent._pool._decide``;
      (b) the bypass-path ``_resume_decision(..., role=self.config.role)`` call
          inside ``Agent._process``, driven with the pool disabled.

    Both use a real ``Agent`` (real config.role, real pool wrapper). Uses the
    established cross-test-module import pattern (cf. test_authz_hook.py) for
    the FakeClient/_make_agent_with_registry harness.
    """

    def test_pool_decide_wrapper_binds_role(self, tmp_path):
        # Seam (a): a FRESH entry recorded under a DIFFERENT agent must NOT
        # resume — the wrapper passes role=butler, so _resume_decision's strict
        # gate returns "new". Omitting role would make a fresh entry "resume".
        from test_agent_process import _make_agent_with_registry
        from session_registry import SessionRegistry
        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        butler = _make_agent_with_registry(reg, role="butler")
        now = datetime.now(timezone.utc)
        fresh = {"agent": "concierge", "sdk_session_id": "cx",
                 "last_active": (now - timedelta(minutes=2)).isoformat()}
        assert butler._pool._decide("voice", fresh, now)[0] == "new"
        # Sanity: a role-MATCHING fresh entry still resumes through the wrapper.
        match = dict(fresh, agent="butler")
        assert butler._pool._decide("voice", match, now)[0] == "resume"

    async def test_bypass_path_binds_role(self, tmp_path, monkeypatch):
        # Seam (b): pool OFF → _process takes _attempt_bypass_turn. A fresh
        # entry at butler's OWN channel_key but under agent="concierge" must
        # start fresh (resume=None). Omitting role in the bypass call would
        # resume the concierge sid → captured_options.resume == "concierge-sid".
        from test_agent_process import FakeClient, _make_agent_with_registry, _msg
        from session_registry import SessionRegistry, build_scoped_session_key
        monkeypatch.setenv("SDK_CLIENT_POOL", "off")
        FakeClient.reset()
        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        key = build_scoped_session_key("voice", "butler", "shared-dev")
        reg._data[key] = {
            "agent": "concierge", "sdk_session_id": "concierge-sid",
            "last_active": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        }
        butler = _make_agent_with_registry(reg, role="butler")
        with patch("sdk_client_pool._default_make_client", FakeClient):
            await butler._process(_msg("voice", "shared-dev", "hi"))
        assert FakeClient.captured_options.resume is None, (
            "bypass path resumed a different agent's session — role not bound"
        )
        # The turn re-registered the key under butler's own identity.
        assert reg.get(key)["agent"] == "butler"


class TestMigration:
    async def test_migrate_moves_text_drops_voice_and_agentless(self, tmp_path):
        from session_registry import SessionRegistry, build_scoped_session_key
        path = tmp_path / "sessions.json"
        path.write_text(json.dumps({
            "telegram-12345": {"agent": "assistant", "sdk_session_id": "a", "last_active": "2026-07-14T00:00:00+00:00"},
            "voice-devbox":   {"agent": "butler",    "sdk_session_id": "b", "last_active": "2026-07-14T00:00:00+00:00"},
            "webhook-uuid":   {"sdk_session_id": "c", "last_active": "2026-07-14T00:00:00+00:00"},  # agentless
        }))
        reg = SessionRegistry(str(path))
        assert reg.migrate_to_v2() == {"migrated": 1, "dropped": 2}
        assert reg.get(build_scoped_session_key("telegram", "assistant", "12345"))["sdk_session_id"] == "a"
        assert all("-v2-" in k for k in reg.all_entries())

    async def test_migrate_idempotent_leaves_v2_data_byte_identical(self, tmp_path):
        # MINOR-1 (review): idempotence must be proven against POPULATED v2
        # data, not an empty registry — a re-run must return zero counts AND
        # leave every already-v2 entry byte-identical.
        from session_registry import SessionRegistry, build_scoped_session_key
        path = tmp_path / "sessions.json"; path.write_text("{}")
        reg = SessionRegistry(str(path))
        v2key = build_scoped_session_key("telegram", "assistant", "999")
        reg._data[v2key] = {
            "agent": "assistant", "sdk_session_id": "x",
            "last_active": "2026-07-14T00:00:00+00:00",
        }
        before = copy.deepcopy(reg._data)
        assert reg.migrate_to_v2() == {"migrated": 0, "dropped": 0}
        assert reg.migrate_to_v2() == {"migrated": 0, "dropped": 0}
        assert reg._data == before
        assert list(reg._data) == [v2key]        # key unchanged, not re-hashed


class TestVoicePoolRoleKeyed:
    def test_two_roles_one_scope_distinct_sessions(self):
        # Pool-KEYING isolation only (VoiceSessionPool is not where resume is
        # decided — that authority is covered by TestResumeAuthorityRoleBound).
        from channels.voice.session import VoiceSessionPool
        pool = VoiceSessionPool(idle_timeout=300)
        a = pool.ensure("dev", role="butler")
        b = pool.ensure("dev", role="concierge")
        assert a.session_key != b.session_key
        assert pool.get("dev", role="butler") is a


class TestWebhookOneshotScopeClassSurvivesV2:
    """Sweeper contract (brief §Step2 note): a v2 webhook one-shot key's hashed
    remainder is never uuid-shaped, so the sweeper must read the persisted
    ``scope_class`` marker rather than re-deriving it from the key."""

    async def test_v2_webhook_oneshot_still_gets_webhook_ttl(self, tmp_path):
        from session_registry import SessionRegistry, build_scoped_session_key
        from session_sweeper import SessionSweeper

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        uuid_scope = "550e8400-e29b-41d4-a716-446655440000"
        key = build_scoped_session_key("webhook", "assistant", uuid_scope)
        await reg.register(key, "assistant", "sid-1", scope_class="webhook_oneshot")

        # Backdate last_active so it's older than the webhook TTL (1 day)
        # but younger than the general session TTL (30 days).
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        reg._data[key]["last_active"] = old

        sweeper = SessionSweeper(
            registry=reg, session_ttl_days=30, webhook_session_ttl_days=1,
        )
        await sweeper._sweep_once()
        assert reg.get(key) is None, "v2 webhook one-shot must be evicted under the SHORT webhook TTL"

    async def test_migrated_v1_webhook_oneshot_swept_under_webhook_ttl(self, tmp_path):
        # IMPORTANT-1 (review): a v1 webhook-<uuid> one-shot aged between the
        # webhook TTL (1d) and the general TTL (30d) must, AFTER migration to a
        # v2 key (whose hash is no longer uuid-shaped), still be evicted under
        # the SHORT webhook TTL — proving migrate_to_v2 stamps scope_class and
        # the sweeper honours it. Without the stamp it would survive to 30 days
        # and, being a one-shot, never re-register to acquire the short TTL.
        from session_registry import SessionRegistry, build_scoped_session_key
        from session_sweeper import SessionSweeper

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        uuid_scope = "550e8400-e29b-41d4-a716-446655440000"
        aged = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        reg._data[f"webhook-{uuid_scope}"] = {
            "agent": "assistant", "sdk_session_id": "sid-1", "last_active": aged,
        }
        assert reg.migrate_to_v2() == {"migrated": 1, "dropped": 0}
        v2key = build_scoped_session_key("webhook", "assistant", uuid_scope)
        assert reg.get(v2key)["scope_class"] == "webhook_oneshot"

        sweeper = SessionSweeper(
            registry=reg, session_ttl_days=30, webhook_session_ttl_days=1,
        )
        await sweeper._sweep_once()
        assert reg.get(v2key) is None, (
            "migrated v1 webhook one-shot must be evicted under the webhook TTL"
        )
