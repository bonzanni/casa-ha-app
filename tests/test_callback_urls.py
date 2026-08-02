"""Task 9 — ``callback_urls``: the validated public base URL + redirect-URI
join used by the callback reconciler (``callback_reconcile._base_url``) and
the ready.json payload (``callback_reconcile._redirect_uri``).

``validated_base`` tightens the bashio ``"null"``/``"None"``/empty guard
casa_core applies to ``PUBLIC_URL`` (see casa_core.py's Telegram-channel
section) into a full absolute-https-origin check: no userinfo, no IP-literal
host, no path/query/fragment. Any failure means the callback facility is
unavailable (``None``), never a partially-trusted value.
"""
from __future__ import annotations

import pytest

import callback_urls

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# validated_base
# ---------------------------------------------------------------------------


def _env(raw: "str | None"):
    return {} if raw is None else {"PUBLIC_URL": raw}


@pytest.mark.parametrize("raw,expected", [
    ("https://casa.example.org", "https://casa.example.org"),
    ("https://casa.example.org/", "https://casa.example.org"),
    ("  https://casa.example.org  ", "https://casa.example.org"),
    ("https://casa.example.org:8443", "https://casa.example.org:8443"),
])
def test_validated_base_accepts_https_origin(raw, expected):
    assert callback_urls.validated_base(_env(raw)) == expected


@pytest.mark.parametrize("raw", [
    "null",
    "None",
    "",
    "   ",
])
def test_validated_base_bashio_guard(raw):
    assert callback_urls.validated_base(_env(raw)) is None


def test_validated_base_missing_env_key():
    assert callback_urls.validated_base({}) is None


def test_validated_base_rejects_http_scheme():
    assert callback_urls.validated_base(_env("http://casa.example.org")) is None


@pytest.mark.parametrize("raw", [
    "https://203.0.113.5",
    "https://203.0.113.5/",
    "https://[2001:db8::1]",
    "https://[2001:db8::1]/",
    "https://[::1]",
])
def test_validated_base_rejects_ip_literal_host(raw):
    assert callback_urls.validated_base(_env(raw)) is None


@pytest.mark.parametrize("raw", [
    "https://casa.example.org/callback",
    "https://casa.example.org/some/path",
])
def test_validated_base_rejects_path(raw):
    assert callback_urls.validated_base(_env(raw)) is None


def test_validated_base_rejects_query():
    assert callback_urls.validated_base(
        _env("https://casa.example.org?x=1")) is None


def test_validated_base_rejects_fragment():
    assert callback_urls.validated_base(
        _env("https://casa.example.org#frag")) is None


@pytest.mark.parametrize("raw", [
    # host deliberately has no dot (not email-shaped) — this test is only
    # about the userinfo rejection, not the host check.
    "https://user@localhost",
    "https://user:pass@localhost",
])
def test_validated_base_rejects_userinfo(raw):
    assert callback_urls.validated_base(_env(raw)) is None


@pytest.mark.parametrize("raw", [
    "casa.example.org",             # not absolute — no scheme
    "//casa.example.org",           # scheme-relative
    "ftp://casa.example.org",
    "not a url at all",
    "https://",                     # scheme but no host
])
def test_validated_base_rejects_non_absolute_or_other_scheme(raw):
    assert callback_urls.validated_base(_env(raw)) is None


def test_validated_base_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://casa.example.org")
    assert callback_urls.validated_base() == "https://casa.example.org"


def test_validated_base_default_missing(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert callback_urls.validated_base() is None


# ---------------------------------------------------------------------------
# redirect_uri
# ---------------------------------------------------------------------------


def test_redirect_uri_exact_construction():
    assert (
        callback_urls.redirect_uri(
            "https://casa.example.org", "plg-gmail--authorize")
        == "https://casa.example.org/callback/plg-gmail--authorize"
    )


def test_redirect_uri_with_port():
    assert (
        callback_urls.redirect_uri(
            "https://casa.example.org:8443", "plg-gmail--authorize")
        == "https://casa.example.org:8443/callback/plg-gmail--authorize"
    )


# ---------------------------------------------------------------------------
# callback_reconcile seam delegation — Task 9 tightens `_base_url()` to call
# `callback_urls.validated_base()` instead of hand-rolling the guard.
# ---------------------------------------------------------------------------


def test_reconcile_base_url_seam_delegates_to_validated_base(monkeypatch):
    """The seam calls ``callback_urls.validated_base`` (module reference, not
    a copied import) — patching the callee is visible from the seam."""
    import callback_reconcile as cr

    monkeypatch.setattr(
        callback_urls, "validated_base", lambda env=None: "sentinel-value")
    assert cr._base_url() == "sentinel-value"


def test_reconcile_base_url_seam_rejects_ip_literal(monkeypatch):
    """A value the OLD guard would have accepted (non-empty, non-null) but
    that is not a valid https origin must now come back None — the seam
    tightening this task exists for."""
    import callback_reconcile as cr

    monkeypatch.setenv("PUBLIC_URL", "https://203.0.113.5")
    assert cr._base_url() is None


def test_reconcile_base_url_seam_rejects_path(monkeypatch):
    import callback_reconcile as cr

    monkeypatch.setenv("PUBLIC_URL", "https://casa.example.org/some/path")
    assert cr._base_url() is None


def test_reconcile_base_url_seam_accepts_valid_origin(monkeypatch):
    import callback_reconcile as cr

    monkeypatch.setenv("PUBLIC_URL", "https://casa.example.org/")
    assert cr._base_url() == "https://casa.example.org"
