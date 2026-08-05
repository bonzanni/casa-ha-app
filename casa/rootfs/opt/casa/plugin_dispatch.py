"""Shared dispatch-target selection for a plugin's registry ``targets``.

Leaf module (stdlib only). Casa dispatches several kinds of casa-authored
turns to a plugin's assigned agent(s) — a post-consent setup tool run
(:mod:`plugin_setup_episodes`), an authorization-callback collection nudge
(:mod:`callback_episodes`), and (a later task) an event-delivery nudge — and
all three pick the SAME execution target off the SAME ``entry["targets"]``
shape (a list of ``"resident:<role>"`` / ``"specialist:<name>"`` strings)
in the SAME order. This module is that one decision, extracted so the three
callers can never drift apart the way ``callback_episodes._compose`` and
``plugin_setup_episodes._compose`` had — by construction, not by convention
— drifted in wording only, never in target order.
"""
from __future__ import annotations


def compose(entry: dict, base: str) -> "tuple[str | None, str]":
    """Deterministic execution-target selection for the instruction *base*.
    Returns ``(role, instruction)`` or ``(None, reason)``.

    Target order: ``resident:assistant`` when targeted; else the
    lexicographically first resident; else the first specialist via
    assistant delegation (the specialist has no channel — the instruction
    names the EXACT specialist and forbids substitution); else no target at
    all.
    """
    targets = entry.get("targets") or []
    residents = sorted(t.split(":", 1)[1] for t in targets
                       if t.startswith("resident:"))
    specialists = sorted(t.split(":", 1)[1] for t in targets
                         if t.startswith("specialist:"))
    if "assistant" in residents:
        return "assistant", base
    if residents:
        return residents[0], base
    if specialists:
        sp = specialists[0]
        return "assistant", (
            f"Delegate to the specialist '{sp}' with the instruction: {base} "
            "Do not substitute another agent.")
    return None, "no resident or specialist target"
