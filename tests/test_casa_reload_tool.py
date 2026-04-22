"""Tests for casa_reload - Supervisor addon restart."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


class TestCasaReloadTool:
    async def test_happy_path_returns_status(self):
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

    async def test_missing_token_returns_error(self):
        from tools import casa_reload
        with patch.dict(os.environ, {}, clear=True):
            result = await casa_reload.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "no_supervisor_token"
