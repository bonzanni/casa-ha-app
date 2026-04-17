"""Tests for memory._render — SessionContext → markdown digest."""

from __future__ import annotations

from dataclasses import dataclass

from memory import _render


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
