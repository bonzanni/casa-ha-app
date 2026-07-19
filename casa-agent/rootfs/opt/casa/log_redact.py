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


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True
