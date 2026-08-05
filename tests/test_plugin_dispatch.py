"""``plugin_dispatch.compose`` — the shared execution-target selection over a
plugin registry entry's ``targets`` list.

Extracted from ``callback_episodes._compose`` and
``plugin_setup_episodes._compose`` (byte-identical target-order behavior in
both): ``resident:assistant`` when targeted, else the lexicographically
first resident, else the first specialist via assistant delegation, else no
target at all.
"""
import plugin_dispatch as pd


def test_resident_assistant_is_preferred_when_present():
    entry = {"targets": ["resident:zeta", "resident:assistant"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "assistant"
    assert instruction == "do the thing"


def test_lexicographically_first_resident_when_no_assistant():
    entry = {"targets": ["resident:mars", "resident:aqua"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "aqua"
    assert instruction == "do the thing"


def test_first_specialist_via_assistant_delegation():
    entry = {"targets": ["specialist:zulu", "specialist:finance"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "assistant"
    # Lexicographically first specialist chosen, delegation instruction
    # names it exactly and forbids substitution.
    assert instruction == (
        "Delegate to the specialist 'finance' with the instruction: do the "
        "thing Do not substitute another agent.")


def test_no_resident_or_specialist_target():
    entry = {"targets": []}
    role, reason = pd.compose(entry, "do the thing")
    assert role is None
    assert reason == "no resident or specialist target"


def test_missing_targets_key_is_no_target():
    entry = {}
    role, reason = pd.compose(entry, "do the thing")
    assert role is None
    assert reason == "no resident or specialist target"


def test_resident_beats_specialist():
    entry = {"targets": ["specialist:finance", "resident:aqua"]}
    role, instruction = pd.compose(entry, "do the thing")
    assert role == "aqua"
    assert instruction == "do the thing"


def test_executor_only_target_is_no_target():
    """A target that is neither ``resident:`` nor ``specialist:`` prefixed
    contributes to neither list."""
    entry = {"targets": ["executor:something"]}
    role, reason = pd.compose(entry, "do the thing")
    assert role is None
    assert reason == "no resident or specialist target"
