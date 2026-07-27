from __future__ import annotations

import re

from markdown_it import MarkdownIt

from canonical_bytes import canonical_text

_ALLOWED = {
    "heading_open", "heading_close", "inline", "paragraph_open",
    "paragraph_close", "bullet_list_open", "bullet_list_close",
    "list_item_open", "list_item_close", "text", "em_open", "em_close",
    "strong_open", "strong_close", "code_inline", "softbreak", "hardbreak",
}
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_RAW_HTML = re.compile(
    r"(?is)<!--|<![A-Z]|<\?|</?[A-Za-z][^>]*>|<[A-Za-z][^\n>]*$"
)
_INLINE_CODE = re.compile(r"(`+)(.*?)(?:\1)", re.DOTALL)


class MarkdownSectionError(ValueError):
    pass


def _iter_tokens(tokens):
    # `MarkdownIt.parse` returns a flat top-level list, but inline tokens
    # (e.g. links, images, emphasis) carry their real structure in
    # `token.children` rather than as sibling top-level tokens — an
    # image or a raw link would otherwise slip past a top-level-only scan.
    # Walk the full tree so nothing unsupported hides inside an "inline".
    for token in tokens:
        yield token
        if token.children:
            yield from _iter_tokens(token.children)


def validate_markdown(source: str) -> str:
    canonical = canonical_text(source)
    source_without_inline_code = _INLINE_CODE.sub("", canonical)
    if _RAW_HTML.search(source_without_inline_code):
        raise MarkdownSectionError("raw HTML is forbidden")
    parser = MarkdownIt("commonmark", {"html": True})
    for token in _iter_tokens(parser.parse(canonical)):
        if token.type in {"html_inline", "html_block"}:
            raise MarkdownSectionError("raw HTML is forbidden")
        if token.type not in _ALLOWED:
            raise MarkdownSectionError(f"unsupported Markdown token: {token.type}")
    return canonical


def sections(source: str) -> list[tuple[int, str, str]]:
    canonical = validate_markdown(source)
    matches = list(_HEADING.finditer(canonical))
    out = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = len(canonical)
        for later in matches[index + 1:]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        body = canonical[match.end():end].strip("\n") + "\n"
        out.append((level, match.group(2).strip(), body))
    return out


def select_markdown_sections(source: str, names: tuple[str, ...]) -> str:
    selected = [body for _, name, body in sections(source) if name in names]
    return "\n".join(body.rstrip("\n") for body in selected) + "\n"
