"""Task 14 / v0.83.0 §2 — engagement-conduct doctrine regression guards.

Plain string-match assertions on the bundled plugin-developer doctrine +
workspace template + the `ask` tool docstring. This wording is read by the
executing agent model at tool-selection time — it is load-bearing, and these
tests catch accidental reverts of the six turn-discipline rules
(ask-then-stop / silent yields / buttons-for-choices / multi / never-pre-number
/ expiry-is-pause) plus the removal of the two old contradictions. Mirrors
`tests/test_assistant_prompts.py` style.
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
        # the framework's refusal error code names the rule.
        assert "embedded_options" in text
        assert "options" in text

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
    for code in ("question_pending", "operator_away", "embedded_options",
                 "unread_inbound", "no_answer"):
        assert code in text, f"{fixture_name} missing error code {code!r}"


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
    assert "embedded_options" in ask_docstring  # A7 refusal disclosed
