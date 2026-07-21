"""Shared adversarial-content marker rejection for authored (operator- or
image-owned) text: persona packs (persona_pack.py) and role artifacts
(role_artifact.py). Both loaders treat their inputs as an adversarial trust
boundary and must reject the same forbidden token classes:

- template/include syntax that could leak into prompt rendering
  (``${...}``, ``{{...}}``, ``{%...%}``, ``{#...#}``, ``!include``)
- Casa's own structural prompt delimiters, which authored content must
  never be able to forge (``<platform_frame>``, ``<role_identity>``,
  ``<persona>``, ``<role_doctrine>``, ``<safety_kernel>`` and their
  closers)
- general HTML tag syntax: ``<``, an optional ``/``, then — after any
  amount of whitespace — a letter, e.g. ``<script>``, ``</div``, ``<img``,
  ``< br``. This is intentionally CONSERVATIVE for a trust boundary: it
  rejects ``<`` followed by a letter even across whitespace, so prose
  like ``a < b`` (comparing against a single-letter word) is rejected
  too, not just literal tag syntax. Authored content must write ``&lt;``
  or avoid a bare ``<`` immediately (modulo whitespace) before a letter.
  Numeric comparisons like ``2 < 3`` remain fine — ``<`` followed by a
  digit never matches this pattern.
"""

from __future__ import annotations

import re

TEMPLATE_MARKERS = (
    "${", "{{", "}}", "{%", "%}", "{#", "#}", "!include",
)
STRUCTURAL_MARKERS = (
    "<platform_frame>", "</platform_frame>", "<role_identity>",
    "</role_identity>", "<persona>", "</persona>", "<role_doctrine>",
    "</role_doctrine>", "<safety_kernel>", "</safety_kernel>",
)
FORBIDDEN_MARKERS = TEMPLATE_MARKERS + STRUCTURAL_MARKERS

# Matches an HTML tag open: '<', optional whitespace, optional '/', optional
# whitespace, then a letter — e.g. "<script", "</div", "<img", "< br". This
# is deliberately conservative: the whitespace is optional on BOTH sides of
# the optional '/', so it ALSO matches prose like "a < b" (space, then a
# letter) — not just literal tag syntax with no space. Numeric comparisons
# like "2 < 3" are unaffected (a digit never matches [A-Za-z]).
HTML_TAG_OPEN_RE = re.compile(r"<\s*/?\s*[A-Za-z]")


def contains_forbidden_marker(text: str) -> bool:
    """True if *text* contains a forbidden template/include/structural
    marker or an HTML tag open."""
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in FORBIDDEN_MARKERS):
        return True
    return bool(HTML_TAG_OPEN_RE.search(text))
