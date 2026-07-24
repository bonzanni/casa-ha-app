"""Shared voice-channel auth helpers for tests (#193, v0.117.0).

The voice SSE/WS handlers are **fail-closed**: a `VoiceChannel` built with no
`webhook_secret` rejects every turn with 401, exactly like `/invoke`,
`/telegram/update` and the voice-agent catalog. That closed the last
unauthenticated path to the butler on the external port.

Most voice tests exercise the voice **pipeline** (streaming, silent-turn
fallback, delegation budget, context sanitization) — not the auth boundary —
and were written against a channel with `webhook_secret=""`. Rather than
sprinkle an HMAC header across ~45 call sites, those fixtures now configure a
real secret and wrap the aiohttp test client in :class:`SigningVoiceClient`,
which signs each request on the way out. Call sites stay untouched.

Tests that deliberately probe the auth boundary (missing/bad signature, the
no-secret rejection itself) must use a RAW client so the wrapper can't mask
the very behaviour under test.
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
from typing import Any

# The secret voice behaviour-test fixtures configure. Any non-empty value
# works; a constant keeps signatures reproducible across files.
VOICE_TEST_SECRET = "unit-voice-secret"


def voice_signature(body: bytes = b"", secret: str = VOICE_TEST_SECRET) -> str:
    """HMAC-SHA256 hex of *body* under *secret* — the scheme `_verify` uses."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class SigningVoiceClient:
    """aiohttp ``TestClient`` wrapper that signs voice requests.

    Delegates everything except ``post``/``ws_connect`` to the wrapped client.
    ``post`` serializes a ``json=`` payload itself so the bytes it SIGNS are
    byte-identical to the bytes it SENDS (aiohttp's own json serializer uses
    different separators, which would produce a signature over different bytes
    and spuriously 401). An explicit ``X-Webhook-Signature`` passed by the
    caller always wins — a test that wants a bad signature still gets one.
    """

    def __init__(self, client: Any, secret: str = VOICE_TEST_SECRET) -> None:
        self._client = client
        self._secret = secret

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _with_signature(self, body: bytes, headers: dict | None) -> dict:
        merged = dict(headers or {})
        merged.setdefault("X-Webhook-Signature",
                          voice_signature(body, self._secret))
        return merged

    async def post(self, path: str, *, json: Any = None, data: Any = None,
                   headers: dict | None = None, **kwargs: Any):
        if json is not None and data is None:
            data = _json.dumps(json).encode()
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")
        body = data if isinstance(data, bytes) else (
            data.encode() if isinstance(data, str) else b"")
        return await self._client.post(
            path, data=data, headers=self._with_signature(body, headers),
            **kwargs)

    def ws_connect(self, path: str, *, headers: dict | None = None,
                   **kwargs: Any):
        # NOT async: the caller does `async with client.ws_connect(...)`, so
        # return aiohttp's context manager unchanged. The WS handler verifies
        # over an EMPTY body.
        return self._client.ws_connect(
            path, headers=self._with_signature(b"", headers), **kwargs)
