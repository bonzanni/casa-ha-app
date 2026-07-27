"""Logging filter that redacts secrets and tokens from log output."""

from __future__ import annotations

import logging
import re

# Patterns that match common secret/token formats
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic-format keys: sk-ant-api03-…, sk-ant-oat01-… — the key format
    # this Claude-powered add-on actually uses. The hyphen after the 3-char
    # "ant" prefix (and after the version segment) defeats the generic sk-
    # pattern below, so match it explicitly. Body may contain '-' and '_'.
    # Keep this BEFORE the generic sk- pattern.
    (re.compile(r"(sk-ant-[a-zA-Z0-9]{2,10}-?)[A-Za-z0-9_-]{8,}"), r"\1***"),
    # Generic long hex/base64 tokens (API keys, OAuth tokens)
    (re.compile(r"(sk-[a-zA-Z0-9]{20})[a-zA-Z0-9]+"), r"\1***"),
    (re.compile(r"(ghp_[a-zA-Z0-9]{4})[a-zA-Z0-9]+"), r"\1***"),
    (re.compile(r"(xox[bpsa]-[a-zA-Z0-9]{4})[a-zA-Z0-9-]+"), r"\1***"),
    # Bearer tokens in headers
    (re.compile(r"(Bearer\s+)[^\s'\"]+", re.IGNORECASE), r"\1***"),
    # Generic password/token/key/secret value patterns in key=value or key: value
    (
        re.compile(
            r"((?:token|key|secret|password|authorization)['\"]?\s*[:=]\s*['\"]?)"
            r"([^\s'\"]{8})[^\s'\"]*",
            re.IGNORECASE,
        ),
        r"\1\2***",
    ),
]


# Exact secret values registered at load (Release A): per-trigger webhook
# secrets are opaque and match no generic pattern, so callers register the
# literal value and every occurrence is masked. Scope is Casa's application
# logging handler only (a plugin process printing to its own stdout is out of
# scope — see spec A2/B4).
_MIN_REGISTERED_LEN = 8
_REGISTERED_SECRETS: set[str] = set()
_REDACTED = "«redacted»"

# Dict keys whose value is a credential and must be masked WHOLESALE when a
# secret rides inside a structured log arg (#214) — independent of the value's
# length or format, so even a short secret_token can't slip past the generic
# patterns / the >=8-char exact-registration floor. Deliberately excludes the
# bare word "key" (benign cache/routing keys like key=voice-latency are common
# and non-secret); only clearly-credential key names match.
_SENSITIVE_KEY_RE = re.compile(
    r"secret|password|passwd|token|authorization|"
    r"api[_-]?key|private[_-]?key|access[_-]?token|client[_-]?secret",
    re.IGNORECASE,
)

# Bound recursion into structured args so a deep or cyclic container can't
# hang the filter; over-depth fails CLOSED (returns the redaction marker).
_MAX_REDACT_DEPTH = 8


def register_secret(value: str) -> None:
    """Register an exact secret value for literal redaction in Casa logs.

    Values shorter than 8 chars are ignored — too short to be a meaningful
    secret and dangerous to blanket-replace.
    """
    if isinstance(value, str) and len(value) >= _MIN_REGISTERED_LEN:
        _REGISTERED_SECRETS.add(value)


def _reset_registered_secrets() -> None:
    """Test hook: clear the registry."""
    _REGISTERED_SECRETS.clear()


def redact(text: str) -> str:
    """Replace known secret patterns and registered exact values in *text*."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    for value in _REGISTERED_SECRETS:
        if value in text:
            text = text.replace(value, _REDACTED)
    return text


def _redact_arg(value: object, _depth: int = 0) -> object:
    """Recursively redact string values inside a log arg.

    Strings are redacted; dicts/lists/tuples are walked so a secret nested
    in a container value is caught too. Anything else is returned as-is.
    Redacting per-*value* (not the rendered ``key=value`` line) keeps
    benign ``key=…`` labels intact — rendering them first would let the
    generic ``key=<8+ chars>`` pattern mangle e.g. ``key=voice-latency``.

    Inside a dict, a value whose KEY names a credential (``secret_token``,
    ``password``, …) is masked wholesale, independent of its length/format —
    this is what actually guarantees a short/opaque ``secret_token`` can't
    leak, since the generic patterns and the >=8-char exact-registration
    floor would both miss a bare short value (#214, Sol review).
    """
    if _depth > _MAX_REDACT_DEPTH:
        return _REDACTED  # fail closed on runaway / cyclic structures
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = _redact_arg(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        redacted = [_redact_arg(v, _depth + 1) for v in value]
        try:
            return type(value)(redacted)
        except Exception:  # noqa: BLE001 — exotic subclass: fail closed to a list
            return redacted
    return value


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from log records.

    Redacts ``record.msg`` and, crucially, walks ``record.args`` recursively
    so a secret riding inside a non-string arg is caught. This matters
    because python-telegram-bot logs Bot API call parameters as a *dict*
    (``"Calling Bot API endpoint `%s` with parameters `%s`", endpoint,
    data`` at ``telegram._bot`` DEBUG) and the ``setWebhook`` ``secret_token``
    sits inside that ``data`` dict (#214). Pre-fix, per-arg redaction only
    touched top-level ``str`` args, so the dict — and its secret — passed
    through untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: _redact_arg(v) for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(_redact_arg(a) for a in record.args)
        except Exception:  # noqa: BLE001 — never let redaction break logging
            pass
        return True
