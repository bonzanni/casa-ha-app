"""Tests for casa_reload - Supervisor addon restart."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def configurator_origin():
    """Set origin_var so the role guard (Bug 7 fix) lets the call through."""
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


class TestCasaReloadTool:
    async def test_happy_path_returns_status(self, configurator_origin):
        from tools import casa_reload
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=None)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session_cm)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        session_cm.post = MagicMock(return_value=fake_resp)
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
            with patch("aiohttp.ClientSession", return_value=session_cm):
                result = await casa_reload.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["supervisor_status"] == 200

    async def test_missing_token_returns_error(self, configurator_origin):
        from tools import casa_reload
        with patch.dict(os.environ, {}, clear=True):
            result = await casa_reload.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "no_supervisor_token"


class TestCasaReloadRoleGuard:
    """Bug 7 (v0.14.6): casa_reload must refuse non-configurator callers
    even if their runtime.yaml::tools.allowed lists it. Defense in depth."""

    async def test_no_origin_no_engagement_refused(self):
        from tools import casa_reload
        # No origin_var, no engagement_var bound — refuse.
        result = await casa_reload.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "not_authorized"

    async def test_assistant_role_refused(self):
        import agent as agent_mod
        from tools import casa_reload
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            result = await casa_reload.handler({})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"
        assert "'assistant'" in payload["message"]

    async def test_specialist_role_refused(self):
        import agent as agent_mod
        from tools import casa_reload
        tok = agent_mod.origin_var.set({"role": "finance"})
        try:
            result = await casa_reload.handler({})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_configurator_engagement_var_path_allowed(self):
        """Engagement-bridge path: engagement_var bound, no origin_var.

        H-1 fix (v0.34.0): when called inside an active engagement,
        casa_reload no longer POSTs Supervisor — it defers to
        ``_finalize_engagement``. Goes past the role guard and returns
        ``deferred: True`` without consulting SUPERVISOR_TOKEN.
        """
        import tools as tools_mod
        from tools import casa_reload, engagement_var
        from engagement_registry import EngagementRecord

        rec = EngagementRecord(
            id="eng-1", kind="executor", role_or_type="configurator",
            driver="claude_code", status="active", topic_id=1,
            started_at=0.0, last_user_turn_ts=0.0,
            last_idle_reminder_ts=0.0, completed_at=None,
            sdk_session_id=None, origin={}, task="",
        )
        tok = engagement_var.set(rec)
        deferred_marker_set: bool
        try:
            with patch.dict(os.environ, {}, clear=True):
                result = await casa_reload.handler({})
            deferred_marker_set = (
                rec.id in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD
            )
        finally:
            engagement_var.reset(tok)
            tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.discard(rec.id)
        payload = json.loads(result["content"][0]["text"])
        # Goes past the role guard (else we'd see not_authorized);
        # defers without POSTing — no SUPERVISOR_TOKEN check needed.
        assert payload["supervisor_status"] == 200
        assert payload["deferred"] is True
        # Marker must be set so _finalize_engagement performs the POST
        # at the end of the engagement.
        assert deferred_marker_set is True
