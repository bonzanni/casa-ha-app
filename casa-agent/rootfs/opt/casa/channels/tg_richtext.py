"""Fail-literal Markdown → Telegram-entity parser (v1).

Recognizes exactly four constructs and nothing else:
  - fenced code blocks  ```` ``` ````            → PRE  (verbatim monospace box)
  - inline code         `code`                   → CODE (isolated single backticks only)
  - bold                **bold**                 → BOLD
  - italic              *italic*                 → ITALIC

Asterisks only — underscores are ALWAYS literal (protects ``mcp__tool__names`` and
snake_case). Emits ``display_text`` with markers removed plus non-crossing spans in
**display-codepoint** offsets. Anything unclosed / ambiguous / unsupported stays
byte-for-byte literal. The parser NEVER raises.

The parser body (``_split_fenced``, ``_scan_emphasis``, ``_inline_code_segments``) was
traced and executed against its adversarial test cases with Sol (Codex) before landing.
"""
from __future__ import annotations

from telegram import MessageEntity

Span = tuple[int, int, str]  # (start, end, kind); kind in pre/code/bold/italic

MAX_LEN = 4096
MAX_ENTITIES = 100

_KIND = {
    "pre": MessageEntity.PRE,
    "code": MessageEntity.CODE,
    "bold": MessageEntity.BOLD,
    "italic": MessageEntity.ITALIC,
}


def parse_markdown(src: str) -> tuple[str, list[Span]]:
    """Return (display_text, spans). Never raises."""
    display_parts: list[str] = []
    spans: list[Span] = []
    pos = 0
    for kind, content in _split_fenced(src):
        if kind == "pre":
            display_parts.append(content)
            spans.append((pos, pos + len(content), "pre"))
            pos += len(content)
        else:
            text, inline = _parse_inline(content)
            display_parts.append(text)
            for s, e, k in inline:
                spans.append((pos + s, pos + e, k))
            pos += len(text)
    spans.sort(key=lambda x: (x[0], -x[1], x[2]))
    return "".join(display_parts), spans


def _split_fenced(src: str) -> list[tuple[str, str]]:
    """Split into ('pre', inner) and ('text', chunk) blocks, line-based.

    A fence opens on a line whose stripped text is ``` optionally followed by a
    language token (no spaces, no backticks) and closes on a line whose stripped
    text is exactly ```. An unclosed fence keeps the opener and remainder literal.
    Separator newlines are preserved as ordinary text, never part of the PRE span.
    """
    lines = src.splitlines(keepends=True)
    blocks: list[tuple[str, str]] = []
    text_buf: list[str] = []
    i = 0

    def body(line: str) -> str:
        return line.rstrip("\r\n")

    def eol(line: str) -> str:
        return line[len(body(line)):]

    def strip_one_eol(text: str) -> str:
        if text.endswith("\r\n"):
            return text[:-2]
        if text.endswith(("\n", "\r")):
            return text[:-1]
        return text

    def is_opener(line: str) -> bool:
        stripped = body(line).strip()
        if not stripped.startswith("```"):
            return False
        tail = stripped[3:]
        return "`" not in tail and not any(ch.isspace() for ch in tail)

    def flush_text() -> None:
        if text_buf:
            blocks.append(("text", "".join(text_buf)))
            text_buf.clear()

    while i < len(lines):
        if not is_opener(lines[i]):
            text_buf.append(lines[i])
            i += 1
            continue
        close = i + 1
        while close < len(lines) and body(lines[close]).strip() != "```":
            close += 1
        if close == len(lines):
            text_buf.extend(lines[i:])  # unclosed → literal remainder
            break
        flush_text()
        blocks.append(("pre", strip_one_eol("".join(lines[i + 1:close]))))
        if close + 1 < len(lines):
            text_buf.append(eol(lines[close]))  # closing newline is ordinary text
        i = close + 1

    flush_text()
    return blocks


def _append_inline_segment(
    segments: list[tuple[bool, str]], is_code: bool, value: str,
) -> None:
    if not value:
        return
    if not is_code and segments and not segments[-1][0]:
        segments[-1] = (False, segments[-1][1] + value)
    else:
        segments.append((is_code, value))


def _inline_code_segments(text: str) -> list[tuple[bool, str]]:
    """Return (is_code, text) segments without pairing across lines.

    Only isolated single backticks are delimiters (a backtick in a run of >=2 is
    literal). Lines with an odd number of isolated delimiters are ambiguous and
    therefore remain fully literal.
    """
    segments: list[tuple[bool, str]] = []

    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]

        isolated: list[int] = []
        i = 0
        while i < len(body):
            if body[i] != "`":
                i += 1
                continue
            run_end = i + 1
            while run_end < len(body) and body[run_end] == "`":
                run_end += 1
            if run_end - i == 1:
                isolated.append(i)
            i = run_end

        if len(isolated) % 2:
            _append_inline_segment(segments, False, line)
            continue

        cursor = 0
        for pair_start in range(0, len(isolated), 2):
            opening = isolated[pair_start]
            closing = isolated[pair_start + 1]
            _append_inline_segment(segments, False, body[cursor:opening])
            _append_inline_segment(segments, True, body[opening + 1:closing])
            cursor = closing + 1
        _append_inline_segment(segments, False, body[cursor:] + ending)

    return segments


def _parse_inline(text: str) -> tuple[str, list[Span]]:
    out: list[str] = []
    spans: list[Span] = []
    for is_code, sub in _inline_code_segments(text):
        if is_code:
            start = len(out)
            out.extend(sub)
            spans.append((start, start + len(sub), "code"))
        else:
            _scan_emphasis(sub, out, spans)
    return "".join(out), spans


def _scan_emphasis(s: str, out: list[str], spans: list[Span]) -> None:
    """Match **bold** / *italic* (asterisks only, nesting) in a code-free run.

    Runs of >=3 asterisks are literal. An unmatched outer delimiter makes matches
    in its suffix literal. Fail-literal; never raises.
    """
    base = len(out)
    stack: list[tuple[str, int]] = []
    pairs: list[tuple[int, int, str]] = []
    i = 0
    while i < len(s):
        if s[i] != "*":
            i += 1
            continue
        j = i + 1
        while j < len(s) and s[j] == "*":
            j += 1
        delim = s[i:j]
        if len(delim) not in (1, 2):
            i = j
            continue
        prev = s[i - 1] if i else ""
        nxt = s[j] if j < len(s) else ""
        can_open = bool(nxt) and not nxt.isspace()
        can_close = bool(prev) and not prev.isspace()
        if can_close and stack and stack[-1][0] == delim:
            _, opening = stack.pop()
            pairs.append((opening, i, delim))
        elif can_open:
            stack.append((delim, i))
        i = j

    if stack:
        cutoff = min(pos for _, pos in stack)
        pairs = [p for p in pairs if p[0] < cutoff and p[1] < cutoff]

    removed: set[int] = set()
    for opening, closing, delim in pairs:
        removed.update(range(opening, opening + len(delim)))
        removed.update(range(closing, closing + len(delim)))
    prefix = [0]
    for pos in range(len(s)):
        prefix.append(prefix[-1] + (pos not in removed))
    out.extend(ch for pos, ch in enumerate(s) if pos not in removed)
    for opening, closing, delim in pairs:
        spans.append((
            base + prefix[opening + len(delim)],
            base + prefix[closing],
            "bold" if delim == "**" else "italic",
        ))


def render(text: str) -> tuple[str, "list[MessageEntity] | None"]:
    """Return (display_text, entities) — entities validated + UTF-16-adjusted, or
    None when the message must be sent plain (no spans, over limits, or invalid)."""
    display, spans = parse_markdown(text)
    if (
        not spans
        or len(text) > MAX_LEN
        or len(display) > MAX_LEN
        or len(spans) > MAX_ENTITIES
    ):
        return display, None
    ents: list[MessageEntity] = []
    for start, end, kind in spans:
        if end <= start or start < 0 or end > len(display):
            return display, None
        ents.append(MessageEntity(type=_KIND[kind], offset=start, length=end - start))
    try:
        return display, MessageEntity.adjust_message_entities_to_utf_16(display, ents)
    except Exception:  # noqa: BLE001 — e.g. an unpaired surrogate breaks UTF-16
        # "never raises": any offset-conversion failure degrades to plain text.
        return display, None
