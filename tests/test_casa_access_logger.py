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


class _FakeRequest:
    """Minimal aiohttp-Request-like object supporting __getitem__.

    aiohttp's ``web.BaseRequest`` behaves as a ``MutableMapping``, which
    ``cid_middleware`` uses via ``request["cid"] = cid``. The real
    ``CasaAccessLogger`` reads that back via ``request["cid"]``, so the
    fake must support subscript access too.
    """

    def __init__(
        self,
        method: str = "GET",
        path: str = "/healthz",
        cid: str | None = None,
    ) -> None:
        self.method = method
        self.path_qs = path
        self.path = path
        self._store: dict[str, str] = {}
        if cid is not None:
            self._store["cid"] = cid

    def __getitem__(self, key: str) -> str:
        return self._store[key]

    def __contains__(self, key: str) -> bool:
        return key in self._store


def _fake_request(
    method: str = "GET", path: str = "/healthz", cid: str | None = None,
) -> _FakeRequest:
    return _FakeRequest(method=method, path=path, cid=cid)


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
    """CasaAccessLogger reads cid from ``request["cid"]`` (set by
    ``cid_middleware`` at ingress) and passes it via ``extra={}`` —
    not from ``cid_var``, which has been reset by the middleware's
    ``finally`` before aiohttp fires ``access_log.log()``.
    """

    def test_cid_from_request_dict(self, caplog):
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request(cid="abcdef01"), _fake_response(), 0.010)
        assert caplog.records[0].cid == "abcdef01"

    def test_missing_request_cid_falls_back_to_dash(self, caplog):
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request(), _fake_response(), 0.010)
        assert caplog.records[0].cid == "-"

    def test_ignores_contextvar(self, caplog):
        """Even if ``cid_var`` happens to be bound (e.g. during a
        nested test), the access logger reads from the request dict.
        This pins the behaviour against the aiohttp lifecycle quirk.
        """
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        token = cid_var.set("deadbeef")
        try:
            caplog.set_level(logging.INFO, logger="casa.access")
            access.log(_fake_request(cid="abcdef01"), _fake_response(), 0.010)
        finally:
            cid_var.reset(token)
        # request["cid"] wins, not cid_var
        assert caplog.records[0].cid == "abcdef01"


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
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("LOG_FORMAT", "json")
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        access.log(_fake_request("POST", "/healthz", cid="facefeed"),
                   _fake_response(200, 5), 0.002)
        captured = capsys.readouterr().out
        last = [line for line in captured.splitlines() if line.strip()][-1]
        payload = json.loads(last)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "casa.access"
        assert payload["cid"] == "facefeed"
        assert "method=POST" in payload["msg"]
        assert "path=/healthz" in payload["msg"]
        assert "status=200" in payload["msg"]
