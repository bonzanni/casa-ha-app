"""Correlation-id logging support (spec 5.2 §7).

Every inbound message gets an 8-char cid at ingress. The bus dispatcher
sets :data:`cid_var` from ``msg.context["cid"]`` before calling the
handler, and :func:`install_logging` installs a LogRecord factory that
tags every record with ``record.cid = cid_var.get()`` at creation time.
Records emitted outside any dispatch (startup, shutdown, sweepers) read
the default value ``"-"``.

``LOG_FORMAT=human`` switches the root handler to the human-readable
format; any other value (including unset) uses :class:`JsonFormatter`.
This is a 5.5 change — prior Casa versions defaulted to human.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import IO

from log_redact import RedactingFilter

# ---------------------------------------------------------------------------
# Context var + cid helpers
# ---------------------------------------------------------------------------

cid_var: ContextVar[str] = ContextVar("cid", default="-")


def new_cid() -> str:
    """Return a fresh 8-char lowercase-hex correlation id."""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Filter — injects cid_var.get() onto every record
# ---------------------------------------------------------------------------


class CidFilter(logging.Filter):
    """Inject ``record.cid`` from the current :data:`cid_var`.

    Standalone utility. :func:`install_logging` does NOT attach this to
    the root logger — it uses :func:`logging.setLogRecordFactory`
    instead, which tags records at creation (before handlers or
    filters run). ``CidFilter`` remains available for callers that
    construct records manually and want to re-inject the current cid.
    Always returns ``True`` — this filter only mutates, it never drops
    records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.cid = cid_var.get()
        return True


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

_HUMAN_FORMAT = "%(asctime)s [%(levelname)s] %(name)s cid=%(cid)s: %(message)s"
_ISO_UTC_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


def _human_formatter() -> logging.Formatter:
    """ISO-UTC human format: ``2026-04-18T14:32:01Z [INFO] name cid=X: msg``."""
    fmt = logging.Formatter(_HUMAN_FORMAT, datefmt=_ISO_UTC_DATEFMT)
    fmt.converter = time.gmtime
    return fmt


class JsonFormatter(logging.Formatter):
    """One-line JSON with fields ``ts, level, logger, cid, msg[, exc]``."""

    def __init__(self) -> None:
        super().__init__(datefmt=_ISO_UTC_DATEFMT)
        self.converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "cid": getattr(record, "cid", "-"),
            "msg": record.message,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Root-logger setup
# ---------------------------------------------------------------------------


def install_logging(
    *, stream: IO[str] | None = None, level: int = logging.INFO,
) -> None:
    """Idempotent root-logger setup.

    - Installs a LogRecord factory that tags every record with
      ``record.cid = cid_var.get()`` at creation. Works for records
      from any logger (Casa, httpx, caplog, …) because the factory
      runs inside ``Logger.makeRecord``, before any handler or filter.
    - Attaches exactly one Casa-owned :class:`logging.StreamHandler`;
      repeated calls remove the previous Casa handler before adding
      the new one.
    - Attaches :class:`log_redact.RedactingFilter` to the new handler
      (not to the root logger — filters on the root logger do NOT run
      for records propagated from descendants).
    - Chooses the formatter from ``LOG_FORMAT`` env: ``json`` → JSON,
      anything else (incl. unset) → human.
    - Quiets the ``httpx`` logger to WARNING (Telegram polling emits a
      line every ~10 s on INFO — retained from the prior basicConfig
      block for behaviour parity).

    Safe to call from tests; the ``_casa_owned`` flag prevents
    duplication across repeated invocations.
    """
    root = logging.getLogger()

    # Remove only Casa-owned handlers so we don't disturb pytest's
    # caplog handler or user-configured handlers.
    for h in list(root.handlers):
        if getattr(h, "_casa_owned", False):
            root.removeHandler(h)

    # Install the factory wrapper exactly once. Repeated install_logging
    # calls must not double-wrap — a double-wrapped factory would
    # mutate record.cid twice (harmless) and risk unbounded nesting on
    # future refactors.
    current_factory = logging.getLogRecordFactory()
    if not getattr(current_factory, "_casa_owned", False):
        orig_factory = current_factory

        def _casa_record_factory(*args, **kwargs):
            record = orig_factory(*args, **kwargs)
            record.cid = cid_var.get()
            return record

        _casa_record_factory._casa_owned = True  # type: ignore[attr-defined]
        _casa_record_factory._wrapped = orig_factory  # type: ignore[attr-defined]
        logging.setLogRecordFactory(_casa_record_factory)

    # Legacy cleanup: earlier iterations attached CidFilter /
    # RedactingFilter to the root logger. Strip those so idempotent
    # re-installs stay clean even after older deployments.
    for f in list(root.filters):
        if getattr(f, "_casa_owned", False):
            root.removeFilter(f)

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler._casa_owned = True  # type: ignore[attr-defined]
    handler.addFilter(RedactingFilter())
    if os.environ.get("LOG_FORMAT", "").strip().lower() == "human":
        handler.setFormatter(_human_formatter())
    else:
        handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
