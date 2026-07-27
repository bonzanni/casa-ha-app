"""Rewrite canonical [tag] syntax to the agent's configured dialect.

Canonical form in agent personalities: [confident], [warm], etc.
Dialects: square_brackets (identity) | parens | none.
"""

from __future__ import annotations

import re

_VALID = ("square_brackets", "parens", "none")

# Leading-only: one or more [...] or (...) atoms at the start of the block,
# each followed by optional whitespace.
_LEADING_TAGS_RE = re.compile(r"^(?:\s*[\[(][^\])]*[\])]\s*)+")
_ANY_SQUARE_TAG_RE = re.compile(r"\[([^\]]+)\]")


class TagDialectAdapter:
    def __init__(self, dialect: str) -> None:
        if dialect not in _VALID:
            raise ValueError(
                f"Invalid tag_dialect {dialect!r}; must be one of {_VALID}"
            )
        self._dialect = dialect

    def render(self, block: str) -> str:
        if self._dialect == "square_brackets":
            return block
        if self._dialect == "parens":
            return _ANY_SQUARE_TAG_RE.sub(lambda m: f"({m.group(1)})", block)
        # 'none' — strip any leading run of [tag] / (tag) atoms
        return _LEADING_TAGS_RE.sub("", block).lstrip()
