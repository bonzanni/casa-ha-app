"""Regression guards on assistant/prompts/system.md content (Phase 5 / E-15).

Plain string-match assertions on the bundled Ellen system prompt. The
prompt isn't a code artifact — it's a YAML-resolved markdown file that
ships with the addon — but the wording is load-bearing for tool-routing
behavior. These tests catch accidental reverts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def _system_md_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / (
        "casa-agent/rootfs/opt/casa/defaults/agents/assistant/prompts/system.md"
    )


def _collapse_ws(text: str) -> str:
    """Collapse whitespace runs (incl. markdown line-wrap newlines) to a
    single space so a VERBATIM prose anchor can be matched regardless of
    where the source file happens to wrap it across lines."""
    return re.sub(r"\s+", " ", text)


@pytest.fixture(scope="module")
def system_md_text() -> str:
    return _system_md_path().read_text(encoding="utf-8")


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


def test_ellen_brief_doctrine_present(system_md_text):
    """W3/Sol B11 regression guard: Ellen's brief-envelope doctrine must be
    present VERBATIM in both executors.yaml cards + system.md, or a future
    edit could silently revert it back to a bare `task=` string — the exact
    failure mode that produced the invoice_reset mistranslation this
    release fixes (a process instruction like "discuss with me first"
    getting paraphrased into a feature requirement instead of landing in
    ``brief.process_requirements`` verbatim).
    """
    executors_path = (
        _system_md_path().parent.parent / "executors.yaml"
    )
    executors_text_raw = executors_path.read_text(encoding="utf-8")
    system_text = _collapse_ws(system_md_text)
    executors_text = _collapse_ws(executors_text_raw)

    doctrine_anchors = [
        "use the `brief` envelope on `engage_executor`",
        "into `brief.process_requirements` VERBATIM",
        "NEVER paraphrase a process instruction into a feature requirement",
        "Set `interaction_required: true` whenever the user asks for "
        "discussion/convergence/review",
        "Relay the executor's completion, which must account for each "
        "acceptance criterion",
    ]

    for anchor in doctrine_anchors:
        assert anchor in system_text, (
            f"system.md missing Ellen brief-envelope doctrine anchor: "
            f"{anchor!r}"
        )
        assert anchor in executors_text, (
            f"executors.yaml missing Ellen brief-envelope doctrine anchor: "
            f"{anchor!r}"
        )

    # Both executor cards must carry the doctrine — not just one card with
    # the other silently exempted from the process-fidelity requirement.
    assert executors_text.count(doctrine_anchors[0]) == 2, (
        "the brief-envelope doctrine must appear on BOTH executor cards "
        "(configurator + plugin-developer) in executors.yaml, found "
        f"{executors_text.count(doctrine_anchors[0])} occurrence(s)"
    )


def test_system_prompt_teaches_protected_tool_challenge_and_relay(system_md_text):
    """v0.76.0 [A:§3.8] doctrine anchor: Ellen's system prompt must carry the
    Sol-accepted protected-tool doctrine VERBATIM (a refused call posts a
    confirmation button; Ellen ends her turn and retries with EXACTLY the
    same arguments on approval), plus her resident-specific relay/re-delegate
    paragraph for a delegated specialist's pending confirmation. A future
    prose rewrite that silently drops or paraphrases this text would leave
    Ellen without the doctrine that keeps a re-tried protected call
    argument-identical (grants are argument-bound — see authz_grants.py)."""
    text = _collapse_ws(system_md_text)
    doctrine_anchors = [
        "your call will be refused and a confirmation button posted to the "
        "user",
        "END YOUR TURN",
        "retry the SAME call with EXACTLY the same arguments",
        "relay that to the user and, after the approval message arrives, "
        "re-delegate the exact same action",
    ]
    for anchor in doctrine_anchors:
        assert anchor in text, (
            f"system.md missing v0.76.0 protected-tool doctrine anchor: "
            f"{anchor!r}"
        )


def test_butler_prompt_teaches_protected_tool_challenge_only():
    """v0.76.0 [A:§3.8] doctrine anchor: the butler prompt gets the
    protected-tool challenge/retry paragraph (butler is a delegate target,
    same as Ellen). It does NOT get the relay/re-delegate paragraph — per
    design §3.8 that paragraph is scoped to Ellen (the assistant), who is
    the one that delegates to specialists; butler's runtime.yaml carries no
    delegate_to_agent/engage_executor, so the relay guidance would be inert
    there."""
    agents_dir = _system_md_path().parent.parent.parent
    butler_path = agents_dir / "butler" / "prompts" / "system.md"
    text = _collapse_ws(butler_path.read_text(encoding="utf-8"))
    doctrine_anchors = [
        "your call will be refused and a confirmation button posted to the "
        "user",
        "END YOUR TURN",
        "retry the SAME call with EXACTLY the same arguments",
    ]
    for anchor in doctrine_anchors:
        assert anchor in text, (
            f"butler system.md missing v0.76.0 protected-tool doctrine "
            f"anchor: {anchor!r}"
        )
    assert "re-delegate the exact same action" not in text, (
        "butler system.md should NOT carry the resident-only relay/"
        "re-delegate paragraph — butler never delegates (per design "
        "§3.8, that paragraph is scoped to Ellen only)."
    )


def test_finance_specialist_prompt_teaches_protected_tool_challenge():
    """v0.76.0 [A:§3.8] doctrine anchor: the finance specialist prompt gets
    the protected-tool challenge/retry paragraph (specialists never relay a
    delegated specialist's confirmation — that's the delegator's job — so
    the relay/re-delegate paragraph is intentionally absent here)."""
    agents_dir = _system_md_path().parent.parent.parent
    finance_path = (
        agents_dir / "specialists" / "finance" / "prompts" / "system.md"
    )
    text = _collapse_ws(finance_path.read_text(encoding="utf-8"))
    doctrine_anchors = [
        "your call will be refused and a confirmation button posted to the "
        "user",
        "END YOUR TURN",
        "retry the SAME call with EXACTLY the same arguments",
    ]
    for anchor in doctrine_anchors:
        assert anchor in text, (
            f"finance system.md missing v0.76.0 protected-tool doctrine "
            f"anchor: {anchor!r}"
        )
    assert "re-delegate the exact same action" not in text, (
        "finance system.md should NOT carry the resident-only relay/"
        "re-delegate paragraph — that's Ellen/butler's job, not the "
        "specialist's."
    )


def test_system_prompt_forbids_engage_executor_context_bleed(system_md_text):
    """O-6 (v0.37.9): Ellen's ``engage_executor`` ``task=`` arg must
    carry ONLY the new task description — not the cumulative
    conversation context with prior tasks bleeding through.

    Live evidence: 2026-05-14 P27.2 cid ``093a02c7`` — Ellen's single
    turn spawned BOTH configurator AND plugin-developer engagements,
    and the configurator engagement received P27.1's rename task
    description instead of P27.2's repo creation task. Cause: Ellen's
    SDK conversation history carried the prior task into the new
    engage_executor call. This guard catches accidental revert of the
    prompt-side mitigation.
    """
    text = system_md_text.lower()
    # Anchor phrase from the v0.37.9 prompt fix — match the spirit of the
    # rule without over-binding the exact wording so editorial polish
    # remains possible.
    assert "only" in text and "engage_executor" in text, (
        "system prompt must include ONLY-the-new-task guidance for "
        "engage_executor calls."
    )
    assert (
        "do not carry" in text
        or "do not include" in text
        or "do not bleed" in text
        or "without bleeding" in text
    ), (
        "system prompt must forbid bleeding prior conversation context "
        "into engage_executor's task arg."
    )
