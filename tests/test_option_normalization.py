"""Task 12 / v0.83.0 — A6 (F-LABEL) enumerator stripping + A7 (F-ANCHOR)
embedded-options detection.

Pure-function coverage of the SHARED enumerator grammar (``_ENUMERATOR_RE`` via
``_strip_enumerator`` / ``_count_enumerated_lines``) and its composition into
``_validate_ask_args`` (strip → post-normalization uniqueness + non-emptiness
re-validation) and ``render_ask_body`` (single labelling). Handler-level A7
refusal + retry-short-circuit tests live in ``test_ask_gates.py`` where a real
driver/broker is needed.
"""

from __future__ import annotations

from channels.channel_handlers import (
    _ASK_EMBEDDED_OPTIONS,
    _count_enumerated_lines,
    _embedded_options_payload,
    _strip_enumerator,
    _validate_ask_args,
    render_ask_body,
)


def _body(options, *, question="Pick:", **over):
    base = {"question": question, "options": options, "timeout_s": 60}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# A6 · _strip_enumerator — the framework de-enumeration primitive
# ---------------------------------------------------------------------------


class TestStripEnumerator:
    def test_spaced_letter_dash_stripped(self):
        # The LIVE incident form: ``A — option`` double-labels with Casa's ``1.``.
        assert _strip_enumerator("A — Python MCP + MCPB") == "Python MCP + MCPB"

    def test_unspaced_letter_dash_stripped(self):
        # No space between letter and separator, space after — still an enumerator.
        assert _strip_enumerator("A— Python MCP") == "Python MCP"

    def test_letter_paren_stripped(self):
        assert _strip_enumerator("b) beta") == "beta"

    def test_digit_period_stripped(self):
        assert _strip_enumerator("1. one") == "one"

    def test_two_digit_paren_stripped(self):
        assert _strip_enumerator("12) twelve") == "twelve"

    def test_middle_dot_stripped(self):
        assert _strip_enumerator("2 · dot") == "dot"

    def test_colon_stripped(self):
        assert _strip_enumerator("C: gamma") == "gamma"

    def test_a_la_carte_survives(self):
        # 'A' + ' ' + 'la' — no separator char from the class, so NOT an enumerator.
        assert _strip_enumerator("A la carte") == "A la carte"

    def test_be_right_back_survives(self):
        assert _strip_enumerator("Be right back") == "Be right back"

    def test_plain_label_untouched(self):
        assert _strip_enumerator("Python MCP") == "Python MCP"

    def test_only_one_enumerator_stripped(self):
        # At most ONE leading enumerator; a second stays (it is the content).
        assert _strip_enumerator("1. 2. still here") == "2. still here"

    def test_marker_only_normalizes_empty(self):
        assert _strip_enumerator("A — ") == ""
        assert _strip_enumerator("1. ") == ""

    def test_whitespace_only_normalizes_empty(self):
        assert _strip_enumerator("   ") == ""


# ---------------------------------------------------------------------------
# A7 · _count_enumerated_lines — the anchor embedded-options detector
# ---------------------------------------------------------------------------


class TestCountEnumeratedLines:
    def test_two_spaced_lines_counted(self):
        # The LIVE spaced ``A — opt`` form must be caught (Sol r1-11).
        q = "Which stack?\nA — Python MCP + MCPB\nB — Rust bridge"
        assert _count_enumerated_lines(q) == 2

    def test_two_digit_lines_counted(self):
        q = "Which?\n1. one\n2. two"
        assert _count_enumerated_lines(q) == 2

    def test_one_enumerated_line(self):
        q = "Which?\nA — Python MCP\njust some prose"
        assert _count_enumerated_lines(q) == 1

    def test_plain_prose_zero(self):
        assert _count_enumerated_lines(
            "What database name do you want to use for this?") == 0

    def test_marker_only_lines_not_counted(self):
        # Bare marker lines carry no \\S content after the enumerator.
        assert _count_enumerated_lines("Q:\nA —\nB —") == 0


# ---------------------------------------------------------------------------
# A6 · _validate_ask_args — strip composes onto Task 10/11 validation
# ---------------------------------------------------------------------------


class TestValidateAskArgsNormalization:
    def test_str_options_de_enumerated(self):
        out = _validate_ask_args(
            _body(["A — Python MCP + MCPB", "B — Rust bridge"]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Python MCP + MCPB", "Rust bridge"]
        assert shorts == [None, None]

    def test_single_labeling_in_render(self):
        out = _validate_ask_args(
            _body(["A — Python MCP + MCPB", "B — Rust bridge"]))
        labels = out[1]
        rendered = render_ask_body(1, "Which stack?", labels)
        assert "1. Python MCP + MCPB" in rendered
        assert "2. Rust bridge" in rendered
        # No double-labelling: the agent's ``A —`` never survives.
        assert "A —" not in rendered

    def test_mixed_enumerator_forms(self):
        out = _validate_ask_args(
            _body(["1. one", "b) beta", "12) twelve", "2 · dot"]))
        assert out[1] == ["one", "beta", "twelve", "dot"]

    def test_a_la_carte_survives_validation(self):
        out = _validate_ask_args(_body(["A la carte", "Be right back"]))
        assert out is not None
        assert out[1] == ["A la carte", "Be right back"]

    def test_post_strip_collision_refused(self):
        # Raw-unique (``A — Same`` != ``1. Same``) but collide after stripping.
        assert _validate_ask_args(_body(["A — Same", "1. Same"])) is None

    def test_marker_only_option_refused(self):
        assert _validate_ask_args(_body(["A — ", "keep"])) is None

    def test_whitespace_only_option_refused(self):
        assert _validate_ask_args(_body(["   ", "keep"])) is None

    def test_dict_labels_and_shorts_normalized(self):
        out = _validate_ask_args(_body([
            {"label": "A — Python MCP", "short": "1. py"},
            {"label": "B — Rust", "short": "2. rs"},
        ]))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Python MCP", "Rust"]
        assert shorts == ["py", "rs"]

    def test_dict_label_collision_after_strip_refused(self):
        assert _validate_ask_args(_body([
            {"label": "A — Same", "short": "x"},
            {"label": "1. Same", "short": "y"},
        ])) is None

    def test_dict_short_collision_after_strip_refused(self):
        # Raw shorts ``A — s`` / ``1. s`` collide to ``s`` post-strip.
        assert _validate_ask_args(_body([
            {"label": "One", "short": "A — s"},
            {"label": "Two", "short": "1. s"},
        ])) is None

    def test_dict_marker_only_short_refused(self):
        assert _validate_ask_args(_body([
            {"label": "One", "short": "A — "},
            {"label": "Two", "short": "ok"},
        ])) is None

    def test_multi_dict_enumerator_composition_end_to_end(self):
        out = _validate_ask_args(_body(
            [
                {"label": "A — Python MCP", "short": "1. py"},
                {"label": "B — Rust bridge", "short": "2. rs"},
            ],
            question="Which apply?",
            multi=True,
        ))
        assert out is not None
        _q, labels, _t, shorts = out
        assert labels == ["Python MCP", "Rust bridge"]
        assert shorts == ["py", "rs"]
        rendered = render_ask_body(3, "Which apply?", labels)
        assert "1. Python MCP" in rendered and "2. Rust bridge" in rendered


# ---------------------------------------------------------------------------
# A7 · the pinned refusal copy
# ---------------------------------------------------------------------------


class TestEmbeddedOptionsPayload:
    def test_payload_shape_and_copy(self):
        p = _embedded_options_payload()
        assert p["ok"] is False
        assert p["error"] == "embedded_options"
        assert p["message"] == _ASK_EMBEDDED_OPTIONS

    def test_copy_matches_spec(self):
        assert _ASK_EMBEDDED_OPTIONS == (
            "this looks like a multiple-choice question — call ask again passing "
            "the choices as options (the operator gets buttons), or multi: true "
            "if several can apply"
        )
