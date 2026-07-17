"""v0.84.0 (round 4, spec D2 items 1 & 4) — ``resolve_button_labels``, the ONE
pure whole-set button-label resolver, plus its content-free floor telemetry.

Round 3's per-option ELISION LADDER (``_elide_one`` / ``_resolve_label_collisions``
/ ``_short_option_label``) is gone entirely (D2: "Drop the ``_short_option_label``
elision ladder entirely (garbled, inconsistent — the '1 · A —…MCP…MCPB' failure)").
Labels are now MODEL-generated (the agent supplies an optional ``short`` per
option) and resolved with WHOLE-SET semantics: either EVERY option has a usable
short (non-blank after strip, pairwise-distinct, and whose final DECORATED
caption fits the 64-char product contract), in which case the whole set renders
``n · <short>`` verbatim — or the WHOLE set floors to ``Option 1``, ``Option 2``,
… Never mixed (some real shorts + some derived/floored), never mutated, never a
rejection.

``options`` items are either a bare ``str`` (full label, no short) or
``{"label": str, "short": ...}``. The DECORATED caption the resolver validates
is ``f"{i+1} · {short}"`` for a single-select ask, or ``f"☑ {i+1} · {short}"``
for a multi ask (the toggle-many keyboard prepends the checkbox glyph to the
stored caption) — so the SAME short can pass for single-select and fail for
multi at the exact same length (Sol r3-5's decoration-budget-is-an-input point).

``floored_ask_telemetry`` is the CONTENT-FREE log line (Sol r2-7): option count,
floor reason (``no_shorts|blank|dup|too_long``), a short-presence bitmap, and a
bounded hash of the option set — NEVER the option/question text itself.
"""

from __future__ import annotations

from channels.telegram import (
    _ASK_BUTTON_CAPTION_CAP,
    floored_ask_telemetry,
    resolve_button_labels,
)


# ---------------------------------------------------------------------------
# (a) all-shorts-usable → verbatim ``n · <short>`` captions
# ---------------------------------------------------------------------------


def test_all_shorts_usable_returns_verbatim_captions() -> None:
    options = [
        {"label": "Python MCP server, packaged as MCPB", "short": "Py-MCPB"},
        {"label": "Python MCP server via a venv install", "short": "Py-venv"},
        {"label": "A curl-based install skill", "short": "curl-skill"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "1 · Py-MCPB", "2 · Py-venv", "3 · curl-skill",
    ]


def test_all_shorts_usable_multi_also_verbatim_undecorated() -> None:
    # The RETURNED caption is always the undecorated stored form (``n ·
    # <short>``) even for a multi ask — the ☐/☑ glyph is a separate, mutable
    # render-time concern (D2 item 3), never part of the persisted caption.
    options = [
        {"label": "Python MCP server, packaged as MCPB", "short": "Py-MCPB"},
        {"label": "A curl-based install skill", "short": "curl-skill"},
    ]
    assert resolve_button_labels(options, multi=True) == [
        "1 · Py-MCPB", "2 · curl-skill",
    ]


# ---------------------------------------------------------------------------
# (b) one blank short floors the WHOLE set
# ---------------------------------------------------------------------------


def test_one_blank_short_floors_whole_set() -> None:
    options = [
        {"label": "Python MCP server", "short": "Py-MCPB"},
        {"label": "Python venv install", "short": "   "},  # blank after strip
        {"label": "curl install skill", "short": "curl-skill"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "Option 1", "Option 2", "Option 3",
    ]


# ---------------------------------------------------------------------------
# (c) duplicate shorts float the WHOLE set
# ---------------------------------------------------------------------------


def test_duplicate_shorts_floor_whole_set() -> None:
    options = [
        {"label": "Python MCP server", "short": "Setup"},
        {"label": "Python venv install", "short": "Setup"},
        {"label": "curl install skill", "short": "curl-skill"},
    ]
    assert resolve_button_labels(options, multi=False) == [
        "Option 1", "Option 2", "Option 3",
    ]


def test_duplicate_shorts_case_insensitive_floor() -> None:
    # Pairwise-distinct is casefold-insensitive — "Setup"/"setup" collide.
    options = [
        {"label": "Python MCP server", "short": "Setup"},
        {"label": "Python venv install", "short": "setup"},
    ]
    assert resolve_button_labels(options, multi=False) == ["Option 1", "Option 2"]


# ---------------------------------------------------------------------------
# (d) non-string short treated as absent → floor reason ``no_shorts``
# ---------------------------------------------------------------------------


def test_non_string_short_treated_as_absent_floors_no_shorts() -> None:
    options = [
        {"label": "Python MCP server", "short": "Py-MCPB"},
        {"label": "Python venv install", "short": "Py-venv"},
        {"label": "curl install skill", "short": 7},  # non-string — absent
    ]
    assert resolve_button_labels(options, multi=False) == [
        "Option 1", "Option 2", "Option 3",
    ]
    telemetry = floored_ask_telemetry(options)
    assert "reason=no_shorts" in telemetry


def test_bare_str_option_has_no_short_floors_no_shorts() -> None:
    # A bare ``str`` item never carries a short — mixing it with dict items
    # that DO have usable shorts still floors the WHOLE set (never mixed).
    options = [
        "Python MCP server",
        {"label": "Python venv install", "short": "Py-venv"},
    ]
    assert resolve_button_labels(options, multi=False) == ["Option 1", "Option 2"]
    assert "reason=no_shorts" in floored_ask_telemetry(options)


# ---------------------------------------------------------------------------
# (e) 64-char decorated-caption boundary; the SAME short diverges
#     single-select (passes) vs. multi (floors) at the identical length.
# ---------------------------------------------------------------------------


def test_single_select_caption_exactly_64_chars_passes() -> None:
    short = "s" * 60  # "1 · " (4) + 60 == 64
    options = [{"label": "full label text", "short": short}]
    caption = resolve_button_labels(options, multi=False)[0]
    assert len(caption) == _ASK_BUTTON_CAPTION_CAP == 64
    assert caption == f"1 · {short}"


def test_single_select_caption_65_chars_floors() -> None:
    short = "s" * 61  # "1 · " (4) + 61 == 65 > 64
    options = [{"label": "full label text", "short": short}]
    assert resolve_button_labels(options, multi=False) == ["Option 1"]


def test_multi_decorated_caption_exactly_64_passes() -> None:
    short = "s" * 58  # "☑ 1 · " (6) + 58 == 64
    options = [{"label": "full label text", "short": short}]
    caption = resolve_button_labels(options, multi=True)[0]
    assert caption == f"1 · {short}"  # returned caption stays undecorated
    assert len(f"☑ {caption}") == 64


def test_same_short_passes_single_but_floors_multi() -> None:
    # Length chosen so the SINGLE-select decorated caption sits exactly at the
    # 64-char boundary (passes) while the MULTI decorated caption (2 extra
    # chars for "☑ ") pushes past it (floors) — the divergence the decoration
    # budget must catch (Sol r3-5).
    short = "s" * 60
    options = [{"label": "full label text", "short": short}]

    single = resolve_button_labels(options, multi=False)
    multi = resolve_button_labels(options, multi=True)

    assert single == [f"1 · {short}"]
    assert len(single[0]) == 64
    assert multi == ["Option 1"]
    assert len(f"☑ {single[0]}") == 66


# ---------------------------------------------------------------------------
# (f) telemetry: content-free — count/reason/bitmap/hash, NEVER option text
# ---------------------------------------------------------------------------


def test_telemetry_line_has_dimensions_but_no_option_text() -> None:
    options = [
        {"label": "Secret project codename Falcon", "short": "Falcon"},
        {"label": "Secret project codename Osprey", "short": None},
    ]
    telemetry = floored_ask_telemetry(options)

    assert "count=2" in telemetry
    assert "reason=no_shorts" in telemetry
    assert "shorts=" in telemetry
    assert "hash=" in telemetry

    # NEVER the option/question text — not the label, not the short.
    assert "Falcon" not in telemetry
    assert "Osprey" not in telemetry
    assert "codename" not in telemetry
    assert "Secret" not in telemetry


def test_telemetry_bitmap_reflects_short_presence_per_option() -> None:
    options = [
        {"label": "A", "short": "a"},
        {"label": "B", "short": None},
        "C",  # bare str — no short
    ]
    telemetry = floored_ask_telemetry(options)
    assert "shorts=100" in telemetry


def test_telemetry_hash_is_bounded_and_deterministic() -> None:
    options = [{"label": "A", "short": "a"}, {"label": "B", "short": "b"}]
    t1 = floored_ask_telemetry(options)
    t2 = floored_ask_telemetry(options)
    assert t1 == t2  # pure, deterministic
    h1 = t1.split("hash=", 1)[1].split()[0]
    assert len(h1) == 12
    assert all(c in "0123456789abcdef" for c in h1)


def test_telemetry_multi_uses_multi_decoration_budget() -> None:
    # A short that floors under multi decoration but would PASS single-select
    # (verbatim, no floor) must report reason ``too_long`` when telemetry is
    # asked about the multi ask specifically, and no floor reason at all for
    # the single-select ask (it never floored).
    short = "s" * 60
    options = [{"label": "full label text", "short": short}]
    assert "reason=too_long" in floored_ask_telemetry(options, multi=True)
    assert "reason=none" in floored_ask_telemetry(options, multi=False)


# ---------------------------------------------------------------------------
# (g) Sol's live case — no shorts anywhere → floor, never a garbled elision
# ---------------------------------------------------------------------------


def test_sol_live_case_no_shorts_floors_never_elides() -> None:
    options = [
        "Option A — Python MCP server, MCPB packaged",
        "Option B — Python MCP server via venv",
        "Option C — curl-based install skill",
    ]
    labels = resolve_button_labels(options, multi=False)
    assert labels == ["Option 1", "Option 2", "Option 3"]
    for lab in labels:
        assert "MCP" not in lab
        assert "…" not in lab
        assert " · " not in lab


# ---------------------------------------------------------------------------
# Purity / never-raises
# ---------------------------------------------------------------------------


def test_never_raises_on_empty_options() -> None:
    assert resolve_button_labels([], multi=False) == []
    assert "count=0" in floored_ask_telemetry([])


def test_pure_no_mutation_of_input() -> None:
    options = [{"label": "A", "short": "a"}, {"label": "B", "short": "b"}]
    snapshot = [dict(o) for o in options]
    resolve_button_labels(options, multi=False)
    floored_ask_telemetry(options)
    assert options == snapshot
