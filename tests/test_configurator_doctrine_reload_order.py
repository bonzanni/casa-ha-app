"""G-2 (v0.33.0) regression-locker: configurator doctrine recipes must
list the reload step BEFORE emit_completion in their canonical order.

Background — exploration2 (2026-05-01) finding G-2: configurator
narrated `casa_reload(_triggers)(scope)` in completion-summary text but
never tool-called it before `emit_completion`. Trigger + specialist
commits landed schema-valid in YAML but never activated. Reproduced
across P8 (trigger) and P11 (specialist).

Fix shape: invert the doctrine order so the reload tool sits BETWEEN
config_git_commit and emit_completion. The model previously treated
emit_completion as terminal and skipped the reload entirely; making it
NOT-terminal keeps the reload a load-bearing step in the plan.

Test contract: for every recipe that mentions both `casa_reload(...)`
(or `casa_reload_triggers(...)`) and `emit_completion`, the FIRST
occurrence of the reload tool name in the file must precede the FIRST
occurrence of `emit_completion`. Tests fail if any recipe regresses.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_DOCTRINE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "casa-agent"
    / "rootfs"
    / "opt"
    / "casa"
    / "defaults"
    / "agents"
    / "executors"
    / "configurator"
    / "doctrine"
)


def _all_recipe_files() -> list[Path]:
    """Return every recipe markdown file under the configurator doctrine."""
    return sorted((_DOCTRINE_ROOT / "recipes").rglob("*.md"))


def _first_index(haystack: str, needle: str) -> int:
    """Index of first occurrence of needle in haystack, or -1."""
    return haystack.find(needle)


def _first_reload_index(haystack: str) -> int:
    """First index of any reload tool name. -1 if none present."""
    candidates = [
        haystack.find("casa_reload_triggers("),
        haystack.find("casa_reload()"),
        # Some recipes include a bare `casa_reload` mention in commentary
        # without parens — only count tool-call shapes for ordering.
    ]
    real = [i for i in candidates if i >= 0]
    return min(real) if real else -1


def test_completion_md_canonical_order_documents_reload_before_emit():
    """The doctrine reference (completion.md) must teach the inverted
    order so the model's plan places reload BEFORE emit_completion.
    """
    p = _DOCTRINE_ROOT / "completion.md"
    text = p.read_text(encoding="utf-8")

    # The numbered list MUST mention reload as step 2 and emit_completion
    # as step 3 (or the textual equivalent). Locate by literal substrings
    # the model parses.
    reload_step = text.find("**Reload (if needed).**")
    emit_step = text.find("**Emit completion.**")
    commit_step = text.find("**Commit.**")

    assert commit_step >= 0, "completion.md missing **Commit.** step"
    assert reload_step >= 0, "completion.md missing **Reload (if needed).** step"
    assert emit_step >= 0, "completion.md missing **Emit completion.** step"

    assert commit_step < reload_step < emit_step, (
        "completion.md canonical order broken — must be Commit -> Reload -> "
        "Emit completion (G-2 regression). "
        f"Got positions: commit={commit_step}, reload={reload_step}, emit={emit_step}"
    )


def test_reload_md_canonical_order_documents_reload_before_emit():
    """reload.md's order-of-operations section must teach the same."""
    p = _DOCTRINE_ROOT / "reload.md"
    text = p.read_text(encoding="utf-8")

    # Must explicitly state reload happens before emit_completion.
    assert "before** `emit_completion`" in text or (
        "**before** `emit_completion`" in text
    ), (
        "reload.md must explicitly state the reload step happens BEFORE "
        "emit_completion (G-2 regression)"
    )


@pytest.mark.parametrize("recipe_path", _all_recipe_files(),
                         ids=lambda p: str(p.relative_to(_DOCTRINE_ROOT)))
def test_recipe_reload_precedes_emit_completion(recipe_path: Path) -> None:
    """For every recipe that calls both a reload tool and
    emit_completion, the reload tool_use site must be authored BEFORE
    the emit_completion call site.

    This is the textual gate that prevents the v0.32.x doctrine
    regression where the model treats emit_completion as terminal and
    drops the reload tool call entirely. Inverting the order makes
    emit_completion the natural terminal step AFTER the reload has run.
    """
    text = recipe_path.read_text(encoding="utf-8")

    reload_idx = _first_reload_index(text)
    emit_idx = _first_index(text, "emit_completion(")

    if reload_idx < 0 or emit_idx < 0:
        # Recipe doesn't exercise the both-present pattern; skip cleanly.
        # (e.g., prompt/edit.md is a none-reload path; marketplace.md is
        # marketplace-only with no reload; resident/delete.md is a
        # two-engagement flow with no inline tool sequence.)
        pytest.skip("recipe doesn't include both reload and emit_completion calls")

    assert reload_idx < emit_idx, (
        f"{recipe_path.relative_to(_DOCTRINE_ROOT)}: reload tool call at "
        f"index {reload_idx} must precede emit_completion at index {emit_idx} "
        f"(G-2 regression — see completion.md)"
    )
