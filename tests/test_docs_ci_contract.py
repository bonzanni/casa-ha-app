"""The docs CI workflow carries the sync-enforcement steps.

The sync property the corpus machinery provides: a PR that adds a substantial
module, an option, a tool, a route or an s6 unit goes red until the coverage
ledger assigns it; a PR that breaks a pinned invariant goes red in the unit
suite; a PR that moves a documented symbol goes red in the anchor check. This
test pins that the workflow actually runs the pieces — deleting a step from
docs.yml must fail here, not pass silently.
"""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docs.yml"


def test_pin_docs_workflow_runs_verifier_ledger_and_impact():
    """Red case demonstrated: removing the coverage_ledger check line from
    docs.yml fails this test."""
    text = WORKFLOW.read_text()
    # The corpus verifier (anchors, invariant-test bindings, allowlist).
    assert "scripts.verify_docs" in text
    # The code-derived coverage ledger, both directions.
    assert "coverage_ledger.py check" in text
    # Generated navigation is current.
    assert "--check-nav" in text
    # The docs-impact decision on changed paths. It no longer lives inline in
    # the workflow: it is scripts/docs_impact.sh, called by BOTH the workflow
    # and scripts/gate.sh (which is what actually binds, since a CI check
    # reports only after a PR exists and can be merged past — PR #383). Pin
    # every link of that chain, which is stricter than the old check for a flag
    # in the workflow text: a comment could satisfy that, and one nearly did.
    root = WORKFLOW.parents[2]
    assert "docs_impact.sh" in text
    impact = (root / "scripts" / "docs_impact.sh").read_text()
    assert "--impact" in impact
    assert "docs_impact.sh" in (root / "scripts" / "gate.sh").read_text()
    # The operating cards must keep routing agents into the corpus — pinned as
    # the substantive directive pattern, not a bare filename an unrelated
    # sentence could satisfy.
    assert 'docs/README\\.md.{0,20}routing table' in text


def test_pin_operating_cards_carry_the_routing_directive():
    """The properties the workflow's card-check enforces, asserted directly
    over newline-normalised text — cards are wrapped prose, and the first CI
    execution of the unnormalised grep failed on a phrase spanning a line
    break (a check never seen red, failing wrong).

    Red case demonstrated: rewording either phrase in a card fails this test.
    """
    import re

    root = WORKFLOW.parents[2]
    for card in ("CLAUDE.md", "AGENTS.md"):
        flat = " ".join((root / card).read_text().split())
        assert "verifiable from the public commit alone" in flat, card
        assert re.search(r"docs/README\.md.{0,20}routing table", flat), card
