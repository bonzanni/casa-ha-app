"""Supervisor discovery publication for the companion Casa integration."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest


class _Response:
    def __init__(self, status: int, payload: dict | None = None):
        self.status = status
        self._payload = payload or {}

    async def json(self) -> dict:
        return self._payload


class _Session:
    """Small async-context-manager Supervisor client recorder."""

    def __init__(self, responses: list[_Response | Exception]):
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def _request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        next_response = next(self.responses)
        if isinstance(next_response, Exception):
            raise next_response
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=next_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    def get(self, url: str, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._request("POST", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self._request("DELETE", url, **kwargs)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token-for-test")
    secret = tmp_path / "webhook_secret"
    state = tmp_path / "casa-supervisor-discovery.json"
    return secret, state


def _session_factory(session: _Session):
    def factory(*, headers: dict, **_kwargs):
        session.headers = headers
        return session

    return factory


@pytest.mark.asyncio
async def test_publishes_exact_schema_one_record_with_runtime_hostname(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    secret.write_text("test-secret")
    session = _Session([
        _Response(200, {"data": {"hostname": "casa-runtime"}}),
        _Response(200, {"data": {"uuid": "new-uuid"}}),
    ])

    await publish_or_remove_discovery(
        auth_enabled=True,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=AsyncMock(),
    )

    assert session.headers == {"Authorization": "Bearer token-for-test"}
    assert session.calls == [
        ("GET", "http://supervisor/addons/self/info", {}),
        ("POST", "http://supervisor/discovery", {
            "json": {
                "service": "casa",
                "config": {
                    "schema_version": 1,
                    "host": "casa-runtime",
                    "port": 18065,
                    "webhook_secret": "test-secret",
                },
            },
        }),
    ]
    assert json.loads(state.read_text()) == {"uuid": "new-uuid"}


@pytest.mark.asyncio
async def test_retries_transient_failure_a_bounded_number_of_times(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    secret.write_text("test-secret")
    session = _Session([
        aiohttp.ClientConnectionError("temporarily unavailable"),
        _Response(503),
        _Response(200, {"data": {"hostname": "casa-runtime"}}),
        _Response(200, {"data": {"uuid": "new-uuid"}}),
    ])
    sleep = AsyncMock()

    await publish_or_remove_discovery(
        auth_enabled=True,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=sleep,
    )

    assert [call[:2] for call in session.calls] == [
        ("GET", "http://supervisor/addons/self/info"),
        ("GET", "http://supervisor/addons/self/info"),
        ("GET", "http://supervisor/addons/self/info"),
        ("POST", "http://supervisor/discovery"),
    ]
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_retries_a_timed_out_supervisor_request(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    secret.write_text("test-secret")
    session = _Session([
        asyncio.TimeoutError(),
        _Response(200, {"data": {"hostname": "casa-runtime"}}),
        _Response(200, {"data": {"uuid": "new-uuid"}}),
    ])
    sleep = AsyncMock()

    await publish_or_remove_discovery(
        auth_enabled=True,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=sleep,
    )

    assert [call[:2] for call in session.calls] == [
        ("GET", "http://supervisor/addons/self/info"),
        ("GET", "http://supervisor/addons/self/info"),
        ("POST", "http://supervisor/discovery"),
    ]
    assert sleep.await_count == 1


@pytest.mark.asyncio
async def test_republishes_matching_record_in_place_with_the_same_uuid(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    secret.write_text("test-secret")
    state.write_text('{"uuid":"existing-uuid"}')
    session = _Session([
        _Response(200, {"data": {"hostname": "casa-runtime"}}),
        _Response(200, {"data": {"uuid": "existing-uuid"}}),
    ])

    await publish_or_remove_discovery(
        auth_enabled=True,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=AsyncMock(),
    )

    assert [call[:2] for call in session.calls] == [
        ("GET", "http://supervisor/addons/self/info"),
        ("POST", "http://supervisor/discovery"),
    ]
    assert json.loads(state.read_text()) == {"uuid": "existing-uuid"}


@pytest.mark.asyncio
async def test_secret_rotation_posts_in_place_without_delete(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    secret.write_text("new-secret")
    state.write_text('{"uuid":"old-uuid"}')
    session = _Session([
        _Response(200, {"data": {"hostname": "casa-runtime"}}),
        _Response(200, {"data": {"uuid": "old-uuid"}}),
    ])

    await publish_or_remove_discovery(
        auth_enabled=True,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=AsyncMock(),
    )

    assert [call[:2] for call in session.calls] == [
        ("GET", "http://supervisor/addons/self/info"),
        ("POST", "http://supervisor/discovery"),
    ]
    assert json.loads(state.read_text()) == {"uuid": "old-uuid"}


@pytest.mark.asyncio
async def test_disabled_auth_deletes_existing_discovery_and_clears_state(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    state.write_text('{"uuid":"existing-uuid"}')
    session = _Session([_Response(200)])

    await publish_or_remove_discovery(
        auth_enabled=False,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=AsyncMock(),
    )

    assert [call[:2] for call in session.calls] == [
        ("DELETE", "http://supervisor/discovery/existing-uuid"),
    ]
    assert not state.exists()


@pytest.mark.asyncio
async def test_disabled_auth_clears_state_when_the_remote_uuid_is_already_stale(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    state.write_text('{"uuid":"stale-uuid"}')
    session = _Session([_Response(404)])

    await publish_or_remove_discovery(
        auth_enabled=False,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=AsyncMock(),
    )

    assert not state.exists()


@pytest.mark.asyncio
async def test_malformed_or_stale_local_uuid_is_overwritten_after_publish(paths):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    secret.write_text("test-secret")
    state.write_text('{"uuid":"stale-uuid"}')
    session = _Session([
        _Response(200, {"data": {"hostname": "casa-runtime"}}),
        _Response(200, {"data": {"uuid": "new-uuid"}}),
    ])

    await publish_or_remove_discovery(
        auth_enabled=True,
        secret_file=secret, state_file=state,
        session_factory=_session_factory(session), sleep=AsyncMock(),
    )

    assert [call[:2] for call in session.calls] == [
        ("GET", "http://supervisor/addons/self/info"),
        ("POST", "http://supervisor/discovery"),
    ]
    assert json.loads(state.read_text()) == {"uuid": "new-uuid"}


@pytest.mark.asyncio
async def test_secret_never_appears_in_logs_or_persisted_state(paths, caplog):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    secret.write_text("canary-secret")
    session = _Session([
        _Response(200, {"data": {"hostname": "casa-runtime"}}),
        _Response(500), _Response(500), _Response(500),
    ])

    with caplog.at_level(logging.WARNING):
        await publish_or_remove_discovery(
            auth_enabled=True,
            secret_file=secret, state_file=state,
            session_factory=_session_factory(session), sleep=AsyncMock(),
        )

    assert "canary-secret" not in caplog.text
    assert not state.exists()


@pytest.mark.asyncio
async def test_enabled_auth_with_unreadable_secret_retains_discovery_state(paths, caplog):
    from supervisor_discovery import publish_or_remove_discovery

    secret, state = paths
    state.write_text('{"uuid":"existing-uuid"}')
    session = _Session([])

    with caplog.at_level(logging.WARNING):
        await publish_or_remove_discovery(
            auth_enabled=True,
            secret_file=secret, state_file=state,
            session_factory=_session_factory(session), sleep=AsyncMock(),
        )

    assert session.calls == []
    assert json.loads(state.read_text()) == {"uuid": "existing-uuid"}
    assert "existing-uuid" not in caplog.text


def test_setup_wires_discovery_after_webhook_secret_selection():
    script = Path("casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh").read_text()
    secret_block = script.index('SECRET_FILE="$DATA_DIR/webhook_secret"')
    publisher = script.index("supervisor_discovery.py")
    assert publisher > secret_block


def test_setup_normalizes_blank_bashio_webhook_secret_before_generation():
    script = Path("casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh").read_text()
    start = script.index('SECRET_FILE="$DATA_DIR/webhook_secret"')
    end = script.index("# Publish Casa's authenticated endpoint", start)
    secret_block = script[start:end]

    assert 'if [ "$USER_SECRET" = "null" ]; then' in secret_block
    assert 'USER_SECRET=""' in secret_block
    assert '[ "$(cat "$SECRET_FILE" 2>/dev/null)" = "null" ]' in secret_block
    assert 'CASA_DISCOVERY_AUTH_ENABLED="$DISCOVERY_AUTH_ENABLED"' in script


def test_manifest_and_docs_declare_discovery_contract():
    manifest = Path("casa-agent/config.yaml").read_text()
    docs = Path("casa-agent/DOCS.md").read_text()
    assert "discovery: [casa]" in manifest
    assert "/data/casa-supervisor-discovery.json" in docs
