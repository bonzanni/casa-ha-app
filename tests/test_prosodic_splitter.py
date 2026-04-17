"""Spec §5.1 + §11 — ProsodicSplitter behaviour.

The splitter is delta-fed: it receives token *suffixes* and emits whole
blocks (prosodic units) at sentence/paragraph boundaries, treating any
bracket span ([…], (…), {…}, <…>) as opaque.
"""

import time

from channels.voice.prosodic import ProsodicSplitter


class TestSentenceBoundaries:
    def test_dot_flushes(self):
        s = ProsodicSplitter()
        assert s.feed("Done.") == ["Done."]

    def test_bang_flushes(self):
        assert ProsodicSplitter().feed("Great!") == ["Great!"]

    def test_question_flushes(self):
        assert ProsodicSplitter().feed("Yes?") == ["Yes?"]

    def test_ellipsis_flushes(self):
        assert ProsodicSplitter().feed("Hmm…") == ["Hmm…"]

    def test_comma_does_not_flush(self):
        s = ProsodicSplitter()
        assert s.feed("Well, ") == []
        assert s.feed("okay.") == ["Well, okay."]

    def test_paragraph_break_flushes(self):
        s = ProsodicSplitter()
        assert s.feed("First line\n\nsecond") == ["First line"]

    def test_multiple_sentences_in_one_feed(self):
        s = ProsodicSplitter()
        assert s.feed("Hi. Done!") == ["Hi.", "Done!"]


class TestTagOpacity:
    def test_period_inside_square_bracket_does_not_flush(self):
        s = ProsodicSplitter()
        assert s.feed("[confident.point] Done.") == ["[confident.point] Done."]

    def test_period_inside_parens_does_not_flush(self):
        s = ProsodicSplitter()
        assert s.feed("(soft.sigh) Goodnight.") == ["(soft.sigh) Goodnight."]

    def test_period_inside_braces_does_not_flush(self):
        s = ProsodicSplitter()
        assert s.feed("{emotion.warm} Hello.") == ["{emotion.warm} Hello."]

    def test_period_inside_angle_does_not_flush(self):
        s = ProsodicSplitter()
        assert s.feed("<mood.flat> Sure.") == ["<mood.flat> Sure."]

    def test_tag_binds_forward(self):
        """[warm] Good morning. is one block, not two."""
        s = ProsodicSplitter()
        assert s.feed("[warm] Good morning.") == ["[warm] Good morning."]

    def test_flush_pushed_past_closing_bracket(self):
        """If a boundary lands inside a tag, it moves to after the close."""
        s = ProsodicSplitter()
        # The '.' in '[warm.rising]' must not trigger a flush.
        assert s.feed("[warm.rising] Hi.") == ["[warm.rising] Hi."]

    def test_unclosed_bracket_does_not_flush(self):
        """Partial tag at end of feed stays in the buffer."""
        s = ProsodicSplitter()
        assert s.feed("[warm") == []
        assert s.feed("] Hi.") == ["[warm] Hi."]


class TestSafetyCap:
    def test_char_cap_fallback_on_clause_mark(self):
        """At 200 chars we break on the rightmost clause mark."""
        s = ProsodicSplitter()
        long = "word, " * 50 + "end"
        out = s.feed(long)
        # At least one block emitted at or before char 200, on a comma.
        assert out, "expected a safety-cap flush"
        assert out[0].endswith(",") or out[0].endswith(";")

    def test_char_cap_hard_cut_if_no_clause_mark(self):
        """No clause mark in 200 chars — hard cut."""
        s = ProsodicSplitter()
        blob = "a" * 250
        out = s.feed(blob)
        assert out
        assert len(out[0]) == 200

    def test_time_cap_honours_boundary(self, monkeypatch):
        """1.5 s wall-clock since last flush forces a break."""
        clock = [0.0]
        monkeypatch.setattr("channels.voice.prosodic.time.monotonic", lambda: clock[0])

        s = ProsodicSplitter()
        assert s.feed("no punct here ") == []
        clock[0] = 2.0  # past the 1.5 s cap
        out = s.feed("more, text")
        # Expect a safety flush on the rightmost clause mark.
        assert out

    def test_time_cap_not_tripped_by_construction_delay(self, monkeypatch):
        """Creating a splitter then waiting for first token must not
        pre-arm the time cap. The 1.5 s window starts when the buffer
        first fills, not at construction.
        """
        clock = [0.0]
        monkeypatch.setattr(
            "channels.voice.prosodic.time.monotonic", lambda: clock[0],
        )
        s = ProsodicSplitter()
        clock[0] = 2.0  # SDK-first-token delay past the cap
        # First real delta arrives. Must NOT immediately emit a cap block.
        assert s.feed("Hello ") == []
        # Now advance another 2 s — cap should fire on the next delta
        # because the buffer has been filling for 2 s.
        clock[0] = 4.0
        out = s.feed("there, more")
        assert out  # the clause-mark-based safety flush fires now

    def test_char_cap_breaks_on_em_dash_when_no_comma(self):
        """Spec §5.1: safety-cap fallback clause marks are `,` `;` and em-dash."""
        s = ProsodicSplitter()
        # 190 chars of 'a', then em-dash+space, then more — char cap at 200.
        blob = ("a" * 190) + " — more continuation padding padding"
        out = s.feed(blob)
        assert out
        # Should break at the em-dash region, not hard-cut
        assert out[0].rstrip().endswith("—") or out[0].rstrip().endswith("— ") \
            or out[0].rstrip().endswith("—")


class TestFinalFlush:
    def test_flush_tail_emits_remainder(self):
        s = ProsodicSplitter()
        s.feed("Unterminated thought")
        tail = s.flush_tail()
        assert tail == "Unterminated thought"

    def test_flush_tail_empty_when_drained(self):
        s = ProsodicSplitter()
        s.feed("Done.")
        assert s.flush_tail() == ""


class TestNonAscii:
    def test_emoji_passthrough(self):
        s = ProsodicSplitter()
        assert s.feed("Hi 👋. Bye.") == ["Hi 👋.", "Bye."]

    def test_non_ascii_passthrough(self):
        s = ProsodicSplitter()
        assert s.feed("Ciao, amico. ¿Sí?") == ["Ciao, amico.", "¿Sí?"]
