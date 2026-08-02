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

    def _invokes(path, script):
        """A real invocation, not a mention. Terra caught the first version of
        this pinning bare substrings: both callers name the script in comments,
        so deleting the executable line still passed. Match a line whose first
        word is the script (optionally via `bash`), with comments excluded."""
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            words = stripped.split()
            if words[0] == script or (words[0] == "bash" and len(words) > 1
                                      and words[1] == script):
                return True
        return False

    assert _invokes(WORKFLOW, "scripts/docs_impact.sh"), \
        "docs.yml must CALL scripts/docs_impact.sh, not merely mention it"
    assert _invokes(root / "scripts" / "gate.sh", "scripts/docs_impact.sh"), \
        "gate.sh must CALL scripts/docs_impact.sh — it is the binding copy"
    # And the script itself must still drive the verifier's impact mode.
    assert "--impact" in (root / "scripts" / "docs_impact.sh").read_text()
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
