"""Publish Casa's authenticated endpoint to the Supervisor discovery API.

The UUID is deliberately the only local discovery state. Reposting the
service updates its Supervisor record in place, so a secret is never copied
into a local state file or a log message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp


_LOGGER = logging.getLogger(__name__)
_SUPERVISOR_URL = "http://supervisor"
_SECRET_FILE = Path("/data/webhook_secret")
_STATE_FILE = Path("/data/casa-supervisor-discovery.json")
_RETRY_DELAYS_S = (0.1, 0.5, 1.0)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _read_uuid(state_file: Path) -> str | None:
    try:
        value = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    uuid = value.get("uuid") if isinstance(value, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


def _write_uuid(state_file: Path, uuid: str) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=state_file.parent, prefix=f".{state_file.name}.",
    )
    try:
        with os.fdopen(fd, "w") as temporary:
            json.dump({"uuid": uuid}, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, state_file)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _clear_state(state_file: Path) -> None:
    try:
        state_file.unlink()
    except FileNotFoundError:
        pass


async def _request(
    session: Any,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[int | None, dict[str, Any]]:
    """Make a bounded, secret-safe Supervisor request."""
    for attempt, delay in enumerate(_RETRY_DELAYS_S):
        try:
            request = getattr(session, method.lower())
            kwargs = {"json": payload} if payload is not None else {}
            async with request(f"{_SUPERVISOR_URL}{path}", **kwargs) as response:
                status = response.status
                if status not in _TRANSIENT_STATUS:
                    if 200 <= status < 300:
                        try:
                            data = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            data = {}
                        return status, data if isinstance(data, dict) else {}
                    _LOGGER.warning("Supervisor discovery %s failed (status=%s)", method, status)
                    return status, {}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            status = None

        if attempt == len(_RETRY_DELAYS_S) - 1:
            _LOGGER.warning("Supervisor discovery %s failed after bounded retries", method)
            return status, {}
        await sleep(delay)

    raise AssertionError("unreachable")


async def publish_or_remove_discovery(
    *,
    auth_enabled: bool,
    secret_file: Path = _SECRET_FILE,
    state_file: Path = _STATE_FILE,
    session_factory: Callable[..., Any] = aiohttp.ClientSession,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Publish the authenticated Casa endpoint, or remove it when disabled."""
    uuid = _read_uuid(state_file)
    if not auth_enabled:
        if uuid is None:
            _clear_state(state_file)
            return
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            _LOGGER.warning("Supervisor discovery unavailable: no Supervisor token")
            return
        async with session_factory(
            headers={"Authorization": f"Bearer {token}"}, timeout=_REQUEST_TIMEOUT,
        ) as session:
            status, _ = await _request(
                session, "DELETE", f"/discovery/{uuid}", sleep=sleep,
            )
            if status == 404 or (status is not None and 200 <= status < 300):
                _clear_state(state_file)
        return

    try:
        secret = secret_file.read_text().strip()
    except OSError:
        secret = ""
    if not secret:
        _LOGGER.warning("Supervisor discovery unavailable: webhook secret missing")
        return

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        _LOGGER.warning("Supervisor discovery unavailable: no Supervisor token")
        return

    async with session_factory(
        headers={"Authorization": f"Bearer {token}"}, timeout=_REQUEST_TIMEOUT,
    ) as session:

        status, addon_info = await _request(
            session, "GET", "/addons/self/info", sleep=sleep,
        )
        hostname = addon_info.get("data", {}).get("hostname") if status == 200 else None
        if not isinstance(hostname, str) or not hostname:
            _LOGGER.warning("Supervisor discovery unavailable: add-on hostname missing")
            return

        config = {
            "schema_version": 1,
            "host": hostname,
            "port": 18065,
            "webhook_secret": secret,
        }
        _, created = await _request(
            session, "POST", "/discovery",
            payload={"service": "casa", "config": config}, sleep=sleep,
        )
        new_uuid = created.get("data", {}).get("uuid")
        if isinstance(new_uuid, str) and new_uuid:
            _write_uuid(state_file, new_uuid)
        else:
            _LOGGER.warning("Supervisor discovery publish returned no UUID")


async def _main() -> None:
    await publish_or_remove_discovery(
        auth_enabled=os.environ.get("CASA_DISCOVERY_AUTH_ENABLED") == "true",
    )


if __name__ == "__main__":
    asyncio.run(_main())
