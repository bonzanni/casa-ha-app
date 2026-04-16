"""Logging filter that redacts secrets and tokens from log output."""

from __future__ import annotations

import logging
import re

# Patterns that match common secret/token formats
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
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


def redact(text: str) -> str:
    """Replace known secret patterns in *text* with masked versions."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
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
