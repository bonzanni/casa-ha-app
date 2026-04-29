"""Tests for memory._render — SessionContext → markdown digest."""

from __future__ import annotations

from dataclasses import dataclass, field

from memory import _render, _render_peer_context


@dataclass
class FakeMessage:
    peer_name: str
    content: str


@dataclass
class FakeSummary:
    content: str


@dataclass
class FakeContext:
    messages: list[FakeMessage]
    summary: FakeSummary | None = None
    peer_representation: str | None = None
    peer_card: list[str] | None = None


@dataclass
class FakePeerContext:
    """Duck-typed stand-in for Honcho's peer.context() return shape.

    Only ``peer_card`` (list[str]) and ``representation`` (str | None) —
    matches what `_render_peer_context` reads (spec §4)."""

    peer_card: list[str] = field(default_factory=list)
    representation: str | None = None


def test_empty_context_returns_empty_string():
    out = _render(FakeContext(messages=[]))
    assert out == ""


def test_messages_only():
    ctx = FakeContext(messages=[
        FakeMessage("nicola", "hi"),
        FakeMessage("assistant", "hello"),
    ])
    out = _render(ctx)
    assert "## Recent exchanges" in out
    assert "[nicola] hi" in out
    assert "[assistant] hello" in out
    assert "## Summary so far" not in out
    assert "## My perspective" not in out
    assert "## What I know about you" not in out


def test_all_sections_present():
    ctx = FakeContext(
        messages=[FakeMessage("nicola", "hi")],
        summary=FakeSummary("we talked about lights"),
        peer_representation="User values brevity.",
        peer_card=["prefers Celsius", "lives in Amsterdam"],
    )
    out = _render(ctx)
    assert "## What I know about you" in out
    assert "- prefers Celsius" in out
    assert "- lives in Amsterdam" in out
    assert "## Summary so far" in out
    assert "we talked about lights" in out
    assert "## My perspective" in out
    assert "User values brevity." in out
    assert "## Recent exchanges" in out
    assert "[nicola] hi" in out


def test_peer_card_alone():
    ctx = FakeContext(messages=[], peer_card=["likes oat milk"])
    out = _render(ctx)
    assert out.startswith("## What I know about you")
    assert "- likes oat milk" in out
    assert "## Recent exchanges" not in out


def test_summary_alone():
    ctx = FakeContext(messages=[], summary=FakeSummary("prior context"))
    out = _render(ctx)
    assert "## Summary so far" in out
    assert "prior context" in out


def test_peer_representation_alone():
    ctx = FakeContext(messages=[], peer_representation="User prefers Dutch.")
    out = _render(ctx)
    assert "## My perspective" in out
    assert "User prefers Dutch." in out


def test_empty_peer_card_omitted():
    ctx = FakeContext(messages=[], peer_card=[])
    out = _render(ctx)
    assert "## What I know about you" not in out
    assert out == ""


def test_render_peer_context_omits_empty_sections():
    """Empty peer.context() return → empty string, no placeholder.

    Spec § 5.3: section omission rules parity with `_render`'s
    no-placeholder doctrine."""
    out = _render_peer_context(FakePeerContext(), observer_role="finance")
    assert out == ""


def test_render_peer_context_renders_peer_card_only():
    """Populated peer_card, empty representation → bullets header
    section only, with the cross-role-disambiguating heading."""
    ctx = FakePeerContext(peer_card=["likes espresso", "freelances at Lesina"])
    out = _render_peer_context(ctx, observer_role="finance")
    assert "## What Finance knows about you (cross-role)" in out
    assert "- likes espresso" in out
    assert "- freelances at Lesina" in out


def test_render_peer_context_renders_representation_only():
    """Empty peer_card, populated representation → representation
    text under the heading (no bullets section)."""
    ctx = FakePeerContext(
        representation="User prioritizes Q2 invoicing over personal expenses"
    )
    out = _render_peer_context(ctx, observer_role="finance")
    assert "## What Finance knows about you (cross-role)" in out
    assert "User prioritizes Q2 invoicing over personal expenses" in out
    assert "- " not in out  # no bullets


def test_render_peer_context_renders_both_sections():
    """Populated peer_card + representation → bullets header section
    followed by the representation text on a new paragraph."""
    ctx = FakePeerContext(
        peer_card=["likes espresso"],
        representation="User prioritizes Q2 invoicing",
    )
    out = _render_peer_context(ctx, observer_role="finance")
    assert "## What Finance knows about you (cross-role)" in out
    assert "- likes espresso" in out
    assert "User prioritizes Q2 invoicing" in out
    # the representation is appended after the peer_card section, not nested under it
    assert out.index("likes espresso") < out.index("User prioritizes")
