"""Canonical Honcho session-id builder.

Honcho's server validates workspace/peer/session ids against
``^[A-Za-z0-9_-]+$`` (upstream ``src/schemas/api.py:37``,
applied via ``Field(pattern=…, min_length=1, max_length=100)`` on
``WorkspaceCreate``/``PeerCreate``/``SessionCreate``). ``:`` is
rejected; we hyphenate parts and fail-fast on any character outside
the regex, since silent sanitization is what blinded us for 11 days
(see ``docs/superpowers/specs/2026-04-28-honcho-session-id-format-design.md``).
"""

from __future__ import annotations

import re

_HONCHO_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HONCHO_NAME_MAX = 100  # mirrors Honcho's max_length=100


def honcho_session_id(*parts: str) -> str:
    """Build a Honcho-compliant session id by joining ``parts`` with ``-``.

    Every part must:
      - be a non-empty ``str``
      - match ``[A-Za-z0-9_-]+`` (ASCII letters, digits, underscore, hyphen)

    The joined result must satisfy Honcho's server-side
    ``^[A-Za-z0-9_-]+$`` regex and be ≤ 100 chars.

    Raises ``ValueError`` on any violation. Silent sanitization is
    intentionally NOT supported.
    """
    if not parts:
        raise ValueError("honcho_session_id requires at least one part")
    for i, part in enumerate(parts):
        if not isinstance(part, str):
            raise ValueError(
                f"part {i} must be str, got {type(part).__name__}"
            )
        if not part:
            raise ValueError(f"part {i} is empty")
        if not _HONCHO_NAME_RE.fullmatch(part):
            raise ValueError(
                f"part {i}={part!r} contains characters outside "
                f"[A-Za-z0-9_-] (Honcho server rejects)"
            )
    joined = "-".join(parts)
    if len(joined) > _HONCHO_NAME_MAX:
        raise ValueError(
            f"session id {joined!r} is {len(joined)} chars; "
            f"Honcho rejects > {_HONCHO_NAME_MAX}"
        )
    return joined
