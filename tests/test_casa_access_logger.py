"""Unit tests for CasaAccessLogger (spec 5.5 §3.2.2–3)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from casa_core_middleware import CasaAccessLogger
from log_cid import cid_var, install_logging


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_request(method: str = "GET", path: str = "/healthz") -> SimpleNamespace:
    """Minimal aiohttp-Request-like object for the logger."""
    return SimpleNamespace(method=method, path_qs=path, path=path)


def _fake_response(status: int = 200, body_length: int = 42) -> SimpleNamespace:
    return SimpleNamespace(status=status, body_length=body_length)


# ---------------------------------------------------------------------------
# TestFormat
# ---------------------------------------------------------------------------


class TestFormat:
    def test_line_contains_method_path_status_duration_bytes(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request("POST", "/invoke/assistant"),
                   _fake_response(200, 117), 1.842)
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "method=POST" in msg
        assert "path=/invoke/assistant" in msg
        assert "status=200" in msg
        assert "duration_ms=1842" in msg
        assert "bytes=117" in msg

    def test_path_includes_query_string(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request("GET", "/probe?x=1&y=2"),
                   _fake_response(200, 0), 0.005)
        msg = caplog.records[0].getMessage()
        assert "path=/probe?x=1&y=2" in msg


# ---------------------------------------------------------------------------
# TestCidInRecord
# ---------------------------------------------------------------------------


class TestCidInRecord:
    def test_cid_matches_contextvar(self, caplog, monkeypatch):
        # install_logging installs the LogRecord factory that tags
        # record.cid from cid_var at creation time.
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        token = cid_var.set("abcdef01")
        try:
            caplog.set_level(logging.INFO, logger="casa.access")
            access.log(_fake_request(), _fake_response(), 0.010)
        finally:
            cid_var.reset(token)
        assert caplog.records[0].cid == "abcdef01"

    def test_no_binding_gives_dash(self, caplog):
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request(), _fake_response(), 0.010)
        assert caplog.records[0].cid == "-"


# ---------------------------------------------------------------------------
# TestLoggerWiring
# ---------------------------------------------------------------------------


class TestLoggerWiring:
    def test_emits_on_casa_access_logger(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request(), _fake_response(), 0.001)
        assert caplog.records[0].name == "casa.access"
        assert caplog.records[0].levelno == logging.INFO


# ---------------------------------------------------------------------------
# TestJsonMode
# ---------------------------------------------------------------------------


class TestJsonMode:
    def test_json_line_parses_with_fields(
        self, caplog, monkeypatch, capsys
    ):
        # Use install_logging with LOG_FORMAT=json, emit via the real
        # StreamHandler, and capture the printed line via capsys.
        monkeypatch.setenv("LOG_FORMAT", "json")
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        token = cid_var.set("facefeed")
        try:
            access.log(_fake_request("POST", "/healthz"),
                       _fake_response(200, 5), 0.002)
        finally:
            cid_var.reset(token)
        captured = capsys.readouterr().out
        last = [line for line in captured.splitlines() if line.strip()][-1]
        payload = json.loads(last)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "casa.access"
        assert payload["cid"] == "facefeed"
        assert "method=POST" in payload["msg"]
        assert "path=/healthz" in payload["msg"]
        assert "status=200" in payload["msg"]
