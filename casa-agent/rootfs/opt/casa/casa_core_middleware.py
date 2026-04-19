"""HTTP middleware + access logger plumbing for Casa (spec 5.5 §3).

This module owns two pieces of aiohttp wiring that are logically
distinct from the request handlers in `casa_core`:

- ``cid_middleware`` — allocates a correlation id (cid) at ingress,
  stores it on the request, and binds the module-level
  ``log_cid.cid_var`` for the handler's task. Spawned tasks inherit
  the binding via asyncio's contextvars snapshot.
- ``CasaAccessLogger`` — emits one log line per request through
  Casa's configured root handler, so access lines share the 5.2-H
  formatter (human or JSON), carry the current ``cid``, and go
  through ``RedactingFilter``.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

from aiohttp import web

from log_cid import cid_var, new_cid


# ---------------------------------------------------------------------------
# cid middleware
# ---------------------------------------------------------------------------


_CID_SHAPE = re.compile(r"^[0-9a-f]{8,32}$")


def _normalise_header(raw: str | None) -> str | None:
    """Return a valid lowercase-hex cid from a header value, or None.

    Accepts 8-32 hex chars, case-insensitive. Anything else → None,
    which signals the middleware to allocate a fresh cid.
    """
    if not raw:
        return None
    candidate = raw.strip().lower()
    if _CID_SHAPE.match(candidate):
        return candidate
    return None


@web.middleware
async def cid_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Bind ``cid_var`` and ``request["cid"]`` for every inbound request.

    Reads ``X-Request-Cid`` if present and well-shaped; otherwise calls
    :func:`log_cid.new_cid`. The binding is scoped via a ContextVar
    token that is reset in ``finally`` regardless of handler outcome.
    """
    cid = _normalise_header(request.headers.get("X-Request-Cid")) or new_cid()
    request["cid"] = cid
    token = cid_var.set(cid)
    try:
        return await handler(request)
    finally:
        cid_var.reset(token)


# ---------------------------------------------------------------------------
# access logger
# ---------------------------------------------------------------------------


from aiohttp.abc import AbstractAccessLogger  # noqa: E402 (import-by-feature)


class CasaAccessLogger(AbstractAccessLogger):
    """Emit one access-log line through Casa's 5.2-H formatter.

    The parent class's ``self.logger`` is set by ``AppRunner`` when the
    access logger is constructed. We ignore aiohttp's default CLF
    formatting entirely and emit a single structured-key-value string
    via ``logger.info``; the installed root handler formats in the
    active mode (human/JSON) and runs the line through
    ``RedactingFilter``.

    **cid source** — aiohttp fires ``access_log.log()`` from the
    connection task AFTER the request-handler task has completed, so
    by that point ``cid_middleware``'s ``finally`` has already reset
    ``cid_var`` back to ``"-"``. We therefore read the cid from
    ``request["cid"]`` (the middleware stored it on the request dict
    at ingress) and re-bind ``cid_var`` for the duration of the
    ``logger.info()`` call; the LogRecord factory then tags
    ``record.cid`` with the right value at creation time.
    """

    def __init__(self, logger: "logging.Logger", log_format: str = "") -> None:
        super().__init__(logger, log_format)

    def log(self, request, response, time) -> None:
        duration_ms = int(time * 1000)
        path = getattr(request, "path_qs", request.path)
        status = getattr(response, "status", 0)
        body_length = getattr(response, "body_length", 0)
        msg = (
            f"access method={request.method} path={path} "
            f"status={status} duration_ms={duration_ms} bytes={body_length}"
        )
        try:
            cid = request["cid"]
        except (KeyError, TypeError):
            cid = "-"
        # Re-bind cid_var so the LogRecord factory tags the new record
        # with the request's cid. ``extra={"cid": ...}`` is rejected by
        # Python's logging module because the factory has already set
        # ``record.cid`` (Python refuses to let ``extra`` overwrite
        # existing record attributes).
        token = cid_var.set(cid)
        try:
            self.logger.info(msg)
        finally:
            cid_var.reset(token)
