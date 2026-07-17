"""Task 14 / v0.83.0 §2, extended round-4 Task B2 (D2-D5) — engagement-conduct
doctrine regression guards.

Plain string-match assertions on the bundled plugin-developer doctrine +
workspace template + the `ask` tool docstring. This wording is read by the
executing agent model at tool-selection time — it is load-bearing, and these
tests catch accidental reverts of the six turn-discipline rules
(ask-then-stop / silent yields / buttons-for-choices / multi / never-pre-number
/ expiry-is-pause) plus the removal of the two old contradictions. Mirrors
`tests/test_assistant_prompts.py` style.

Round-4 additions (spec D2-D5, Task B2): the round-3 `embedded_options`
regex-gate error code is retired (Task B1 deletes the runtime machinery; D3
makes options-vs-prose a fail-open DOCTRINE rule, not a framework refusal),
so the doctrine text — and these tests — no longer name that error code.
Added instead: (a) the inline-parenthetical "(a) ... (b) ..." counter-example
for options-not-prose; (b) a prominent `short`-per-option steer; (c) an
explicit no-pre-labeling/no-self-numbering rule (options AND the question);
(d) the sharpened silent-yield rule quoting the exact observed violation line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_DEV = _ROOT / (
    "casa-agent/rootfs/opt/casa/defaults/agents/executors/plugin-developer"
)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.fixture(scope="module")
def conduct_text() -> str:
    return _collapse_ws(
        (_DEV / "doctrine/engagement-conduct.md").read_text(encoding="utf-8")
    ).lower()


@pytest.fixture(scope="module")
def template_text() -> str:
    return _collapse_ws(
        (_DEV / "workspace-template/CLAUDE.md.tmpl").read_text(encoding="utf-8")
    ).lower()


@pytest.fixture(scope="module")
def conventions_raw() -> str:
    return (_DEV / "doctrine/casa-conventions.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ask_docstring() -> str:
    import inspect

    import sys

    sys.path.insert(0, str(_ROOT / "casa-agent/rootfs/opt/casa"))
    from channels import casa_engagement_channel as cec

    return (inspect.getdoc(cec.ask) or "").lower()


# --- the SIX rules present in BOTH doctrine and template --------------------


@pytest.mark.parametrize("fixture_name", ["conduct_text", "template_text"])
class TestSixRulesPresent:
    def test_rule1_ask_then_stop(self, fixture_name, request):
        text = request.getfixturevalue(fixture_name)
        # every operator decision is an ask, and the turn ENDS after asking.
        assert "ask" in text
        assert "end your turn" in text
        # never a reply for a decision / never answer your own question.
        assert "never" in text and ("reply" in text or "answer your own" in text)

    def test_rule2_end_silently(self, fixture_name, request):
        text = request.getfixturevalue(fixture_name)
        assert "silent" in text  # "silently" / "end turns silently"

    def test_rule3_buttons_for_choices(self, fixture_name, request):
        text = request.getfixturevalue(fixture_name)
        # round 4 (D3): options-vs-prose is doctrine-only, fail-open — the
        # old `embedded_options` framework refusal is retired (Task B1); the
        # rule itself must still be unambiguous without it.
        assert "options" in text
        assert "must go in" in text or "must go in `options`" in text \
            or "never as prose" in text or "never prose" in text

    def test_rule4_multi(self, fixture_name, request):
        text = request.getfixturevalue(fixture_name)
        assert "multi: true" in text

    def test_rule5_never_prenumber_short_labels(self, fixture_name, request):
        text = request.getfixturevalue(fixture_name)
        assert "never pre-number" in text or "never pre-number/letter" in text \
            or "casa numbers" in text
        assert "short" in text  # the {"label", "short"} readable-button escape

    def test_rule6_expiry_is_pause(self, fixture_name, request):
        text = request.getfixturevalue(fixture_name)
        assert "no_answer" in text
        assert "pause" in text  # "paused" / "PAUSED"
        assert "operator_away" in text
        assert "re-ask" in text  # "never re-ask"


# --- the framework error codes the doctrine must name ----------------------


@pytest.mark.parametrize("fixture_name", ["conduct_text", "template_text"])
def test_framework_error_codes_referenced(fixture_name, request):
    text = request.getfixturevalue(fixture_name)
    # round 4 (D3): `embedded_options` is retired — options-vs-prose is now
    # doctrine-only, fail-open (Task B1 deletes the refusal machinery).
    for code in ("question_pending", "operator_away",
                 "unread_inbound", "no_answer"):
        assert code in text, f"{fixture_name} missing error code {code!r}"


@pytest.mark.parametrize("fixture_name", ["conduct_text", "template_text"])
def test_embedded_options_error_code_retired(fixture_name, request):
    """Round 4 (D3): the regex gate + `embedded_options` refusal payload are
    deleted (Task B1); the doctrine must no longer promise a framework
    refusal that no longer exists — options-vs-prose is fail-open doctrine."""
    text = request.getfixturevalue(fixture_name)
    assert "embedded_options" not in text


# --- ABSENCE of the two old contradictions ---------------------------------


def test_template_drops_old_first_contact_open_questions(template_text):
    """Sol r1-10: the old `CLAUDE.md.tmpl` first-contact told the FIRST reply to
    CONTAIN open questions (reserving ask for choice questions) — a direct
    contradiction of ask-then-stop. The rewrite must remove it."""
    assert "and open questions (use" not in template_text


def test_conventions_drops_ask_via_reply(conventions_raw):
    """Sol r4-7: casa-conventions' `Operator approval` copy directed the agent to
    `ask the operator directly via mcp__casa-engagement-channel__reply` — a
    decision must be an `ask`, never a `reply`. The fix points at the ask tool."""
    assert "directly via `mcp__casa-engagement-channel__reply`" not in conventions_raw
    assert "mcp__casa-engagement-channel__reply`" not in _collapse_ws(
        conventions_raw)


# --- ask docstring carries the pause + silence contract --------------------


def test_ask_docstring_pause_and_silence_contract(ask_docstring):
    assert "no_answer" in ask_docstring
    assert "pause" in ask_docstring        # engagement PAUSED
    assert "silent" in ask_docstring       # end your turn silently
    assert "re-ask" in ask_docstring       # do NOT re-ask
    # round 4 (D3): the `embedded_options` refusal is retired — the docstring
    # must not claim a framework refusal that no longer exists.
    assert "embedded_options" not in ask_docstring


# --- round 4 (D2-D5, Task B2) additions --------------------------------------


_SILENT_YIELD_COUNTEREXAMPLE = (
    "the ask posted as an open-ended anchor. i'll end my turn and wait for "
    "the operator's answer before proceeding."
)


@pytest.mark.parametrize("fixture_name", ["conduct_text", "template_text"])
def test_options_not_prose_inline_parenthetical_counterexample(
    fixture_name, request,
):
    """D3: enumerable answers MUST go in `options`, never as prose in a
    free-text anchor — with an inline-parenthetical "(a) ... (b) ..."
    counter-example so the rule survives a doctrine-only, fail-open world
    (no regex gate catches this anymore; Task B1)."""
    text = request.getfixturevalue(fixture_name)
    assert "must go in" in text or "never as prose" in text \
        or "never prose" in text
    assert "(a)" in text and "(b)" in text


def test_ask_docstring_options_not_prose_inline_parenthetical_counterexample(
    ask_docstring,
):
    assert "must go in" in ask_docstring or "never as prose" in ask_docstring
    assert "(a)" in ask_docstring and "(b)" in ask_docstring


@pytest.mark.parametrize("fixture_name", ["conduct_text", "template_text"])
def test_short_per_option_steer(fixture_name, request):
    """D2: doctrine must steer the agent to supply a `short` for any
    non-trivial option, not just mention the escape-hatch shape exists."""
    text = request.getfixturevalue(fixture_name)
    assert "short" in text
    assert "couple of words" in text or "non-trivial" in text


def test_ask_docstring_short_is_prominent(ask_docstring):
    """D2: the ask tool description makes `short` an OBVIOUS, steered path —
    not a buried afterthought. It must appear in the first ~500 chars (right
    alongside the `options` shape description) and be phrased as guidance
    the agent should follow, not just documented shape."""
    assert "short" in ask_docstring
    lead = ask_docstring[:500]
    assert "short" in lead, (
        "`short` must be prominent (near the top of the docstring), "
        "not buried after unrelated detail"
    )
    assert "supply" in ask_docstring or "couple of words" in ask_docstring


@pytest.mark.parametrize("fixture_name", ["conduct_text", "template_text"])
def test_no_pre_labeling_options_or_question(fixture_name, request):
    """D4: doctrine must forbid BOTH pre-labeling options ("Option A —") and
    self-numbering the question ("Q7:") — Casa numbers everything, and
    neither the enumerator-strip nor the Q-prefix-strip exist anymore
    (Task B1 deletes both; text now posts verbatim)."""
    text = request.getfixturevalue(fixture_name)
    assert "option a" in text or "option a — " in text
    assert "q7:" in text
    assert "never pre-" in text or "never pre-number" in text \
        or "never pre-label" in text


def test_ask_docstring_no_pre_labeling_options_or_question(ask_docstring):
    assert "q7:" in ask_docstring
    assert "never pre-" in ask_docstring


@pytest.mark.parametrize("fixture_name", ["conduct_text", "template_text"])
def test_sharpened_silent_yield_exact_counterexample(fixture_name, request):
    """D5: the end-turns-silently rule must quote the EXACT observed
    sign-off line as the counter-example — not a paraphrase — so the rule
    is unambiguous about what "narrating you're ending your turn" means."""
    text = request.getfixturevalue(fixture_name)
    assert _SILENT_YIELD_COUNTEREXAMPLE in text


def test_conduct_silent_yield_counterexample_is_verbatim():
    """Belt-and-suspenders on the primary doctrine file: assert the exact
    counter-example sentence appears with its original capitalization/
    punctuation intact (not just case-insensitive/whitespace-collapsed),
    since this is the precise line operators observed live."""
    raw = (_DEV / "doctrine/engagement-conduct.md").read_text(encoding="utf-8")
    exact = (
        "The ask posted as an open-ended anchor. I'll end my turn and wait "
        "for the operator's answer before proceeding."
    )
    assert exact in raw
