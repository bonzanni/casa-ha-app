"""Regression guards on assistant/prompts/system.md content (Phase 5 / E-15).

Plain string-match assertions on the bundled Ellen system prompt. The
prompt isn't a code artifact — it's a YAML-resolved markdown file that
ships with the addon — but the wording is load-bearing for tool-routing
behavior. These tests catch accidental reverts.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _system_md_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / (
        "casa-agent/rootfs/opt/casa/defaults/agents/assistant/prompts/system.md"
    )


@pytest.fixture(scope="module")
def system_md_text() -> str:
    return _system_md_path().read_text(encoding="utf-8")


def test_system_prompt_inverts_consult_first_hedge(system_md_text):
    """Phase 5 / E-15: prompt nudges Ellen toward consult_other_agent_memory
    by default. Regression guard against accidental revert."""
    assert "prefer `consult_other_agent_memory` first" in system_md_text
    # Old wording must not return — biased Ellen toward delegate.
    assert "default to `delegate_to_agent`" not in system_md_text


def test_system_prompt_includes_tina_in_examples(system_md_text):
    """E-15 example list includes Tina to extend the cross-role pattern
    beyond Finance/Health."""
    assert "what did Tina mention about lights" in system_md_text


def test_system_prompt_keeps_both_recall_tools_visible(system_md_text):
    """The hedge inversion must NOT remove either tool from the prompt."""
    assert "consult_other_agent_memory" in system_md_text
    assert "delegate_to_agent" in system_md_text


def test_system_prompt_forbids_llm_arithmetic_for_finance(system_md_text):
    """Phase 6 / E-5: Ellen never performs financial arithmetic
    herself; she always delegates to Alex (finance role) and, when
    delegation fails, declines rather than producing an LLM-computed
    answer. Anchor strings are load-bearing — accidental rewording
    that breaks them is the regression this test catches."""
    text = system_md_text.lower()
    # Anchor 1: explicit "never compute" rule.
    assert "never compute" in text or "never perform arithmetic" in text, (
        "Ellen prompt must explicitly forbid LLM arithmetic for "
        "financial figures — anchor phrase missing."
    )
    # Anchor 2: route to Alex / finance.
    assert "alex" in text and "delegate" in text, (
        "Ellen prompt must point at Alex / delegation as the way to "
        "compute financial figures."
    )
    # Anchor 3: explicit decline behavior on delegation failure.
    assert "without alex" in text or "finance is reachable" in text, (
        "Ellen prompt must teach the user-facing decline shape on "
        "delegation failure — anchor phrase missing."
    )
