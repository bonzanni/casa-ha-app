"""Task 9 — the validated public base URL the authorization-callback facility
builds every redirect URI from.

``callback_reconcile._base_url()`` (Task 5's seam) applied only the bashio
``"null"``/``"None"``/empty guard casa_core uses for ``PUBLIC_URL`` — any
other non-empty string, including a bare IP, a URL with a path, or one
carrying credentials, was passed straight through. A callback redirect URI is
handed to a THIRD PARTY (the plugin's OAuth provider) and is later matched
byte-for-byte against what the provider was registered with, so a malformed
base is not merely cosmetic: it can silently mint a redirect URI the
provider rejects, or — worse — resolve to somewhere other than this
container. :func:`validated_base` closes that gap: the result is either a
clean ``https://`` origin with no userinfo, no IP-literal host, and no
path/query/fragment, or ``None`` (the facility is unavailable — every routed
plugin surfaces ``callback_base_url_invalid``, exactly as an unset
``PUBLIC_URL`` already did).
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urljoin, urlsplit

# bashio returns the literal string "null" for an unset optional add-on
# option; casa_core's own PUBLIC_URL guard (the Telegram-channel wiring in
# casa_core.py) additionally treats "None" the same way. Mirrored here
# EXACTLY so the two guards can never drift.
_UNSET_SENTINELS = ("null", "None", "")


def validated_base(env: "dict | None" = None) -> "str | None":
    """The operator's ``PUBLIC_URL``, validated as an absolute ``https://``
    origin — or ``None`` if it is unset or not one.

    ``env`` defaults to :data:`os.environ`; tests pass a plain dict so no
    monkeypatching of the process environment is required.

    Rejects (all ⇒ ``None``):

    * not ``https://`` (including scheme-relative, or no scheme at all)
    * no host, or a host that IS an IP literal (IPv4 dotted-quad or
      bracketed IPv6) — a callback base names a stable DNS origin, not an
      address that can float or collide
    * userinfo (``user[:pass]@``) — never part of a redirect URI
    * a path other than empty or ``"/"`` (the latter only possible before
      the trailing-slash strip below)
    * a query string or a fragment
    * any whitespace or C0/DEL control character anywhere in the value —
      e.g. a glued-in space (``"https:// example.com"``) or an embedded tab
      (review finding, Task 9 fix round 1: checked BEFORE ``urlsplit`` runs,
      since ``urlsplit`` itself silently drops bare CR/LF/TAB from its own
      parse — a check against the parsed host alone would miss exactly the
      characters that make the raw string unsafe to hand back verbatim)
    """
    if env is None:
        env = os.environ
    raw = env.get("PUBLIC_URL", "").strip().rstrip("/")
    if raw in _UNSET_SENTINELS:
        return None
    if _has_stray_whitespace_or_control(raw):
        return None

    parts = urlsplit(raw)
    if parts.scheme != "https":
        return None
    if not parts.netloc:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    host = parts.hostname
    if not host or _is_ip_literal(host):
        return None
    if parts.path not in ("", "/"):
        return None
    if parts.query or parts.fragment:
        return None
    return raw


def _has_stray_whitespace_or_control(value: str) -> bool:
    """True if *value* carries any whitespace or C0/DEL control character.

    A valid https origin never legitimately contains one. This must run
    against the RAW string, not the ``urlsplit``-parsed host: CPython's
    ``urlsplit`` (a hardening against header-injection-style URLs) silently
    strips bare CR/LF/TAB from its own internal parse before it ever reaches
    ``.hostname``, so a plain space survives into the parsed host but an
    embedded tab does not — checking only the parsed host would accept the
    tab case and return the dirty raw string verbatim anyway."""
    return any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in value)


def _is_ip_literal(host: str) -> bool:
    """True for both an IPv4 dotted-quad and a (bracket-stripped, per
    ``urlsplit().hostname``) IPv6 literal — never a real DNS hostname."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def redirect_uri(base: str, effective: str) -> str:
    """The redirect URI registered with / matched against a callback's OAuth
    provider: ``base`` (a :func:`validated_base` result — no trailing slash,
    no path) joined with ``callback/<effective>`` via :mod:`urllib.parse`,
    never hand-rolled string concatenation."""
    return urljoin(f"{base}/", f"callback/{effective}")
