"""Tests for memory._render — SessionContext → markdown digest."""

from __future__ import annotations

from dataclasses import dataclass, field

from memory import _render, _render_peer_context


@dataclass
class FakeMessage:
    """Mirror of honcho-ai v3 Message used for the primary _render path.

    Field name `peer_id` matches the Honcho OpenAPI schema (verified
    Context7 at plan-write 2026-04-29). Pre-Phase-2, this stub used
    `peer_name`, which was a SQLite-shape mirror — that mismatch is why
    M3a's "real-shape coverage" failed to catch E-1 on production
    v0.20.0.
    """
    peer_id: str
    content: str


@dataclass
class FakeSqliteMessage:
    """Mirror of memory._SqliteMsg — peer_name field, no peer_id.

    Used to verify _render's SQLite-fallback branch still works after
    Phase 2's defensive `getattr(m, "peer_id", None) or getattr(m,
    "peer_name", "?")` change in memory.py.
    """
    peer_name: str
    content: str


@dataclass
class FakeSummary:
    content: str


@dataclass
class FakeContext:
    messages: list  # duck-typed: peer_id or peer_name + content
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


def test_render_handles_honcho_v3_message_shape():
    """E-1 regression: real Honcho Message exposes peer_id, not peer_name.

    Before the fix, _render does `m.peer_name` and AttributeErrors on the
    real SDK shape — which is why every butler delegation logged
    'specialist memory read failed for butler' on production v0.20.0.
    """
    ctx = FakeContext(messages=[
        FakeMessage("nicola", "hi"),
        FakeMessage("butler", "hello"),
    ])
    out = _render(ctx)
    assert "## Recent exchanges" in out
    assert "[nicola] hi" in out
    assert "[butler] hello" in out


def test_render_handles_sqlite_message_shape():
    """E-1 regression — SQLite branch of the peer_id/peer_name fallback.

    _SqliteMsg (legacy in-process backend) keeps its `peer_name` field
    after Phase 2; verify _render still routes through the
    fallback branch correctly.
    """
    ctx = FakeContext(messages=[
        FakeSqliteMessage(peer_name="nicola", content="hi"),
    ])
    out = _render(ctx)
    assert "[nicola] hi" in out


def test_render_message_without_either_field_uses_question_mark():
    """E-1 defensive — neither peer_id nor peer_name → '?'.

    Pure paranoia case; documents the second `getattr` default. Any
    future refactor that breaks this assertion must be intentional.
    """
    @dataclass
    class _Anonymous:
        content: str

    ctx = FakeContext(messages=[_Anonymous(content="ghost")])
    out = _render(ctx)
    assert "[?] ghost" in out
