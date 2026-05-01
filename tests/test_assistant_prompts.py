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


def test_executors_yaml_lists_only_real_registered_executor_types():
    """F-6 (v0.32.0) doctrine drift guard.

    The 2026-05-02 exploration session caught Ellen narrating
    ``engagement`` as a third executor type because
    ``defaults/agents/assistant/executors.yaml`` listed it as one.
    There is no ``engagement`` executor in the registry —
    interactive-mode delegation to a specialist is a Tier 2 primitive
    (``delegate_to_agent(mode='interactive')``), conceptually different
    from a Tier 3 executor type.

    This test enumerates real executor directories under
    ``defaults/agents/executors/`` and asserts every ``executor_type``
    listed in Ellen's doctrine matches one of them. Catches future
    additions or removals on either side that drift apart.
    """
    import yaml

    # _system_md_path() = .../defaults/agents/assistant/prompts/system.md
    # parents: prompts → assistant → agents
    agents_dir = _system_md_path().parent.parent.parent
    executors_dir = agents_dir / "executors"
    yaml_path = agents_dir / "assistant" / "executors.yaml"

    real_executor_types = sorted(
        p.name for p in executors_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    doctrine = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    doctrine_types = sorted(
        entry["executor_type"] for entry in doctrine.get("executors", [])
    )

    drift = set(doctrine_types) - set(real_executor_types)
    assert not drift, (
        f"Ellen's executors.yaml doctrine lists executor_type values "
        f"that don't exist in the registry: {sorted(drift)}. "
        f"Real executor directories: {real_executor_types}. "
        f"Either add the missing executor definition or remove the "
        f"doctrine entry."
    )


def test_system_prompt_teaches_sync_vs_interactive_delegation(system_md_text):
    """F-6 (v0.32.0): the engagement-as-executor doctrine drift was fixed
    by deleting that executors.yaml entry and folding its guidance into
    the system prompt. Ellen still needs to know when to use
    ``delegate_to_agent(mode='interactive')`` vs ``mode='sync'``.

    Anchors a small set of phrases so a future prose rewrite can't
    silently drop the distinction.
    """
    text = system_md_text.lower()
    assert "mode='interactive'" in text, (
        "system prompt must teach interactive-mode delegation."
    )
    assert "mode='sync'" in text, (
        "system prompt must teach sync-mode delegation."
    )
    assert "engagements supergroup" in text, (
        "system prompt must reference the Engagements supergroup as "
        "the destination for interactive delegations."
    )
