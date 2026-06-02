"""Canonical Hindsight bank-id builder.

Casa addresses Hindsight memory banks as ``casa-{role}`` (spec §4.1). This
builder fails fast on any id that falls outside a conservative client-side
charset, because the **server does not validate**: probed live against
Hindsight 0.7.1 (2026-06-02), ``casa.finance`` (dot), ``Casa_Butler``
(uppercase/underscore) and a 100-char id were ALL accepted with HTTP 200.
A permissive server is exactly why silent client-side acceptance is the bug
class to avoid (cf. ``honcho_ids.py`` — silent sanitization blinded us for
11 days). We therefore impose our own rule and raise rather than coerce.

Allowed: ASCII letters, digits, underscore, hyphen. ``.``/``/``/space/
non-ASCII are rejected (``/`` and space also break the URL path segment).
"""
from __future__ import annotations

import re

_BANK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Conservative cap. The server accepted 100 chars with no published limit;
# Casa's own names are short (``casa-assistant`` = 14), so 64 is ample and safe.
_BANK_NAME_MAX = 64


def bank_id(*parts: str) -> str:
    """Build a Hindsight bank id by joining ``parts`` with ``-``.

    Every part must be a non-empty ``str`` matching ``[A-Za-z0-9_-]+``.
    Raises ``ValueError`` on any violation or if the joined id exceeds
    ``_BANK_NAME_MAX``. Silent sanitization is intentionally NOT supported.
    """
    if not parts:
        raise ValueError("bank_id requires at least one part")
    for i, part in enumerate(parts):
        if not isinstance(part, str) or not part:
            raise ValueError(f"part {i} must be a non-empty str, got {part!r}")
        if not _BANK_NAME_RE.fullmatch(part):
            raise ValueError(
                f"part {i}={part!r} contains characters outside [A-Za-z0-9_-]"
            )
    joined = "-".join(parts)
    if len(joined) > _BANK_NAME_MAX:
        raise ValueError(
            f"bank id {joined!r} is {len(joined)} chars; max {_BANK_NAME_MAX}"
        )
    return joined
