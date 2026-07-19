"""Fail-literal Markdown → Telegram-entity parser (v2, 2026-07-19 rework).

Recognizes exactly five constructs and nothing else:
  - fenced code blocks  ```` ``` ````            → PRE  (verbatim monospace box)
  - inline code         `code`                   → CODE (isolated single backticks only)
  - bold                **bold**                 → BOLD
  - italic              *italic*                 → ITALIC
  - ATX headings        ## Heading               → BOLD line (hashes stripped)

Asterisks only — underscores are ALWAYS literal (protects ``mcp__tool__names`` and
snake_case). Emits ``display_text`` with markers removed plus non-crossing spans in
**display-codepoint** offsets. Anything unclosed / ambiguous / unsupported stays
byte-for-byte literal. The parser NEVER raises.

v2 (converged with Sol + Terra against the 2026-07-19 prod-leak replay)
replaces the v1 per-code-segment emphasis scan, whose segment-edge flanking
made emphasis adjacent to inline code unmatchable (the dominant literal-``**``
leak):

* ONE emphasis scan per LINE with inline-code regions as opaque atoms —
  delimiters inside code are literal, a code atom is a non-space neighbor for
  flanking, and pairs may enclose code atoms. Emphasis scope is PER-LINE
  (intentional v2 compatibility change — bounds the fail-literal cutoff to one
  line and makes line-boundary page cuts span-safe).
* A line with an ODD isolated-backtick count is FULLY inline-literal — no
  code, no emphasis, no heading (ambiguous ⇒ byte-for-byte literal).
* Bot API nesting rule: bold/italic must NOT contain or intersect CODE/PRE, so
  emphasis and heading spans are SPLIT AROUND code intervals at emission.
* ATX headings (CommonMark-ish): 0-3 leading spaces, 1-6 hashes, then
  whitespace and non-empty content; trailing hash run stripped only when
  whitespace-separated. Anything else (``##foo``, 7+ hashes, 4-space indent,
  bare ``##``) stays literal. Never recognized inside fenced PRE.

``render()`` returns one message; ``render_paged()`` is the delivery planner —
it parses ONCE, cuts the display at preferred boundaries (paragraph → line →
space → hard), clips/rebases spans per page (a span crossing a cut becomes one
entity per page; PRE included), and enforces BOTH the 4096 UTF-16-unit length
budget AND the 100-entity budget per page.
"""
from __future__ import annotations

import bisect
import re

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

_HEADING_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<hashes>#{1,6})(?P<gap>[ \t]+)(?P<rest>.*)$")
_TRAILING_HASH_RE = re.compile(r"[ \t]+#+[ \t]*$")


def parse_markdown(src: str) -> tuple[str, list[Span]]:
    """Return (display_text, spans). Never raises."""
    display_parts: list[str] = []
    spans: list[Span] = []
    pos = 0
    for kind, content in _split_blocks(src):
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


def _split_blocks(src: str) -> list[tuple[str, str]]:
    """Fence split, then confident-table split inside the text blocks."""
    blocks: list[tuple[str, str]] = []
    for kind, content in _split_fenced(src):
        if kind == "pre":
            blocks.append((kind, content))
        else:
            blocks.extend(_split_tables(content))
    return blocks


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


_TABLE_SEP_CELL_RE = re.compile(r"\s*:?-{3,}:?\s*")


def _is_table_block(stripped_rows: list[str]) -> bool:
    """CONFIDENT markdown table: header + `|---|`-style separator, consistent
    column counts, and NO markers inside cells (a backtick, asterisk, or
    escaped pipe makes the block ambiguous — rendering it verbatim as PRE
    would resurrect literal markers, so it stays with the inline pass)."""
    if len(stripped_rows) < 2:
        return False
    for row in stripped_rows:
        if "\\|" in row or "`" in row or "*" in row:
            return False

    def cells(row: str) -> list[str]:
        return row[1:-1].split("|")

    sep = cells(stripped_rows[1])
    if not all(_TABLE_SEP_CELL_RE.fullmatch(c) for c in sep):
        return False
    ncols = len(cells(stripped_rows[0]))
    if len(sep) != ncols:
        return False
    return all(len(cells(row)) == ncols for row in stripped_rows)


def _split_tables(text: str) -> list[tuple[str, str]]:
    """Split ('text', chunk) blocks around confident table blocks → PRE.

    A candidate block is >=2 CONTIGUOUS lines that (stripped) start AND end
    with ``|``; it must pass ``_is_table_block`` or the lines stay ordinary
    text (fail-literal — the inline pass still renders markers inside rows).
    The table renders VERBATIM (no cell reflow) as one PRE span; monospace
    preserves the column alignment Telegram's proportional font destroys.
    The last row's line ending stays ordinary text (mirrors the fence rule).
    """
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[str, str]] = []
    text_buf: list[str] = []

    def flush() -> None:
        if text_buf:
            blocks.append(("text", "".join(text_buf)))
            text_buf.clear()

    i = 0
    while i < len(lines):
        j = i
        while j < len(lines):
            stripped = lines[j].rstrip("\r\n").strip()
            if len(stripped) >= 2 and stripped.startswith("|") and stripped.endswith("|"):
                j += 1
            else:
                break
        rows = [lines[k].rstrip("\r\n").strip() for k in range(i, j)]
        if j - i >= 2 and _is_table_block(rows):
            flush()
            last_body = lines[j - 1].rstrip("\r\n")
            blocks.append(("pre", "".join(lines[i:j - 1]) + last_body))
            ending = lines[j - 1][len(last_body):]
            if ending:
                text_buf.append(ending)
            i = j
        else:
            text_buf.append(lines[i])
            i += 1

    flush()
    return blocks


def _isolated_backticks(body: str) -> list[int]:
    """Positions of isolated single backticks (a backtick in a run of >=2 is
    literal and yields no positions)."""
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
    return isolated


def _scan_emphasis_line(
    body: str, code_regions: list[tuple[int, int]],
) -> list[tuple[int, int, str]]:
    """Match **bold** / *italic* on ONE line with code regions opaque.

    *code_regions* are tick-to-tick source intervals ``(open_tick, close_tick)``
    (inclusive); no delimiter is recognized inside them, but their boundary
    backticks count as non-space flanking context. Runs of >=3 asterisks are
    literal. An unmatched opener makes matches at/after it literal (line-scoped
    fail-literal cutoff). Returns (opening, closing, delim) tuples in line
    coordinates. Never raises.
    """
    stack: list[tuple[str, int]] = []
    pairs: list[tuple[int, int, str]] = []
    n = len(body)
    region_idx = 0
    i = 0
    while i < n:
        while region_idx < len(code_regions) and i > code_regions[region_idx][1]:
            region_idx += 1
        if (
            region_idx < len(code_regions)
            and code_regions[region_idx][0] <= i <= code_regions[region_idx][1]
        ):
            i = code_regions[region_idx][1] + 1
            continue
        if body[i] != "*":
            i += 1
            continue
        j = i + 1
        while j < n and body[j] == "*":
            j += 1
        delim = body[i:j]
        if len(delim) not in (1, 2):
            i = j
            continue
        prev = body[i - 1] if i else ""
        nxt = body[j] if j < n else ""
        can_open = bool(nxt) and not nxt.isspace()
        can_close = bool(prev) and not prev.isspace()
        if can_close and stack and stack[-1][0] == delim:
            _, opening = stack.pop()
            pairs.append((opening, i, delim))
        elif can_open:
            stack.append((delim, i))
        i = j
    if stack:
        cutoff = min(p for _, p in stack)
        pairs = [p for p in pairs if p[0] < cutoff and p[1] < cutoff]
    return pairs


def _subtract_intervals(
    start: int, end: int, holes: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Subtract sorted, disjoint *holes* from ``[start, end)``; drop empties.

    Bisects to the first hole that can overlap so a message with thousands of
    code spans stays O(k log n) per emphasis span, not O(n) (Sol impl-review:
    the linear scan made pathological inputs quadratic overall)."""
    pieces: list[tuple[int, int]] = []
    cur = start
    idx = bisect.bisect_left(holes, (start,))
    if idx:
        idx -= 1  # the previous hole may still overlap ``start``
    for hs, he in holes[idx:]:
        if hs >= end:
            break
        if he <= cur:
            continue
        if hs > cur:
            pieces.append((cur, hs))
        cur = max(cur, he)
    if cur < end:
        pieces.append((cur, end))
    return pieces


def _merge_same_kind(spans: list[Span]) -> list[Span]:
    """Union overlapping (incl. nested/duplicate) same-kind emphasis spans —
    e.g. a heading's line bold over an inner ``**bold**`` emits ONE entity."""
    out: list[Span] = []
    for kind in ("bold", "italic"):
        ranges = sorted((s, e) for s, e, k in spans if k == kind)
        merged: list[list[int]] = []
        for s, e in ranges:
            if merged and s < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out.extend((s, e, kind) for s, e in merged)
    out.extend(sp for sp in spans if sp[2] not in ("bold", "italic"))
    return out


def _parse_inline(text: str) -> tuple[str, list[Span]]:
    """Line-oriented inline pass: headings + inline code + per-line emphasis.

    Marker positions (backtick delimiters, emphasis delimiters, heading
    hashes/indent/trailing run) are collected into a removal set; spans are
    computed in source coordinates and mapped to display coordinates at the
    end. Emphasis/heading spans are split around code intervals (Bot API: a
    bold/italic entity must never contain or intersect CODE).
    """
    removed: set[int] = set()
    code_src: list[tuple[int, int]] = []       # content intervals, source coords
    emph_src: list[tuple[int, int, str]] = []  # content intervals, source coords
    heading_src: list[tuple[int, int]] = []    # content intervals, source coords

    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        isolated = _isolated_backticks(body)
        if len(isolated) % 2:
            pos += len(line)  # ambiguous backticks ⇒ whole line inline-literal
            continue

        code_regions = [
            (isolated[k], isolated[k + 1]) for k in range(0, len(isolated), 2)
        ]
        for o, c in code_regions:
            removed.add(pos + o)
            removed.add(pos + c)
            code_src.append((pos + o + 1, pos + c))

        m = _HEADING_RE.match(body)
        if m and m.group("rest").strip():
            rest = m.group("rest")
            content_end = len(rest)
            tm = _TRAILING_HASH_RE.search(rest)
            if tm and rest[: tm.start()].strip():
                content_end = tm.start()
            prefix = m.end("gap")
            removed.update(range(pos, pos + prefix))
            removed.update(range(pos + prefix + content_end, pos + len(body)))
            heading_src.append((pos + prefix, pos + prefix + content_end))

        for opening, closing, delim in _scan_emphasis_line(body, code_regions):
            removed.update(range(pos + opening, pos + opening + len(delim)))
            removed.update(range(pos + closing, pos + closing + len(delim)))
            emph_src.append((
                pos + opening + len(delim), pos + closing,
                "bold" if delim == "**" else "italic",
            ))
        pos += len(line)

    if not removed:
        return text, []

    # Source → display mapping (prefix sums over kept positions).
    prefix = [0] * (len(text) + 1)
    for i, ch in enumerate(text):
        prefix[i + 1] = prefix[i] + (0 if i in removed else 1)
    display = "".join(ch for i, ch in enumerate(text) if i not in removed)

    spans: list[Span] = []
    code_disp = sorted(
        (prefix[a], prefix[b]) for a, b in code_src if prefix[b] > prefix[a]
    )
    spans.extend((a, b, "code") for a, b in code_disp)

    emph_disp: list[Span] = [
        (prefix[a], prefix[b], kind) for a, b, kind in emph_src
    ]
    emph_disp.extend((prefix[a], prefix[b], "bold") for a, b in heading_src)
    split: list[Span] = []
    for a, b, kind in emph_disp:
        split.extend(
            (x, y, kind) for x, y in _subtract_intervals(a, b, code_disp)
        )
    spans.extend(_merge_same_kind(split))
    return display, spans


# ---------------------------------------------------------------------------
# Delivery: single-message render + the paged delivery planner.
# ---------------------------------------------------------------------------


def _utf16_units(s: str) -> int:
    return len(s) + sum(1 for ch in s if ord(ch) > 0xFFFF)


def _spans_to_entities(
    display: str, spans: list[Span],
) -> "list[MessageEntity] | None":
    """Validate spans against *display* and convert to UTF-16 entities;
    ``None`` on any invalid offset or conversion failure (never raises)."""
    ents: list[MessageEntity] = []
    for start, end, kind in spans:
        if end <= start or start < 0 or end > len(display):
            return None
        ents.append(MessageEntity(type=_KIND[kind], offset=start, length=end - start))
    try:
        return MessageEntity.adjust_message_entities_to_utf_16(display, ents)
    except Exception:  # noqa: BLE001 — e.g. an unpaired surrogate breaks UTF-16
        # "never raises": any offset-conversion failure degrades to plain text.
        return None


def render(text: str) -> tuple[str, "list[MessageEntity] | None"]:
    """Return (display_text, entities) — entities validated + UTF-16-adjusted, or
    None when the message must be sent plain (no spans, over limits, or invalid).

    v2: judged on DISPLAY length — a raw text whose markers push it past 4096
    but whose display fits still renders. Single-message contract; callers
    that may exceed one message use ``render_paged()``."""
    display, spans = parse_markdown(text)
    if (
        not spans
        or len(display) > MAX_LEN
        or _utf16_units(display) > MAX_LEN
        or len(spans) > MAX_ENTITIES
    ):
        return display, None
    return display, _spans_to_entities(display, spans)


def _advance_by_utf16(display: str, start: int, budget: int) -> int:
    """Largest index ``end`` such that ``display[start:end]`` fits *budget*
    UTF-16 units."""
    units = 0
    i = start
    n = len(display)
    while i < n:
        units += 2 if ord(display[i]) > 0xFFFF else 1
        if units > budget:
            return i
        i += 1
    return n


def _preferred_cut(display: str, start: int, end: int) -> int:
    """Pick a cut in ``(start, end]`` preferring paragraph, then line, then
    space boundaries; hard cut at *end* otherwise."""
    para = display.rfind("\n\n", start + 1, end)
    if para > start:
        return para
    line = display.rfind("\n", start + 1, end)
    if line > start:
        return line
    space = display.rfind(" ", start + 1, end)
    if space > start:
        return space
    return end


def _paginate(
    display: str, spans: list[Span], limit: int, max_entities: int,
) -> list[tuple[str, list[Span]]]:
    """Cut *display* into pages within the UTF-16 *limit* AND the entity
    budget; spans are clipped/rebased per page (a span crossing a cut becomes
    one span per page — PRE included). Page-leading newlines are stripped."""
    if _utf16_units(display) <= limit and len(spans) <= max_entities:
        return [(display, spans)]
    spans_sorted = sorted(spans, key=lambda x: (x[0], -x[1], x[2]))
    pre_intervals = [(s, e) for s, e, k in spans_sorted if k == "pre"]
    pages: list[tuple[str, list[Span]]] = []
    start = 0
    n = len(display)
    while start < n:
        end = _advance_by_utf16(display, start, limit)
        cut = n if end >= n else _preferred_cut(display, start, end)
        inter = [sp for sp in spans_sorted if sp[0] < cut and sp[1] > start]
        if len(inter) > max_entities:
            bound = inter[max_entities][0]
            if bound > start:
                cut = min(cut, bound)
                inter = [
                    sp for sp in spans_sorted if sp[0] < cut and sp[1] > start
                ]
            if len(inter) > max_entities:  # degenerate pileup: drop the excess
                inter = inter[:max_entities]
        page_spans = [
            (max(s, start) - start, min(e, cut) - start, k)
            for s, e, k in inter
        ]
        pages.append((
            display[start:cut],
            [p for p in page_spans if p[1] > p[0]],
        ))
        # Swallow AT MOST the one paragraph separator at the cut (bounded — an
        # unbounded skip silently drops content), and NEVER inside a PRE span
        # (blank code lines are meaningful).
        skipped = 0
        while (
            cut < n and display[cut] == "\n" and skipped < 2
            and not any(s <= cut < e for s, e in pre_intervals)
        ):
            cut += 1
            skipped += 1
        if cut <= start:  # progress guard (unreachable in practice)
            cut = start + 1
        start = cut
    return pages


def render_paged(
    text: str,
) -> list[tuple[str, "list[MessageEntity] | None"]]:
    """Delivery planner (v2): parse ONCE, then return ``[(display, entities)]``
    pages that each fit Telegram's 4096 UTF-16-unit and 100-entity budgets.

    Marker-free by construction — a page whose entities degrade (``None``:
    no spans on that page, or UTF-16 conversion failure) still carries its
    DISPLAY slice, never raw source. Callers send each page as one physical
    message; kwargs like ``reply_parameters`` belong on the FIRST page only.
    Never raises."""
    display, spans = parse_markdown(text)
    out: list[tuple[str, "list[MessageEntity] | None"]] = []
    for page_text, page_spans in _paginate(display, spans, MAX_LEN, MAX_ENTITIES):
        if not page_spans:
            out.append((page_text, None))
            continue
        out.append((page_text, _spans_to_entities(page_text, page_spans)))
    return out
