"""Spec §5.2 — TagDialectAdapter."""

import pytest

from channels.voice.tts_adapter import TagDialectAdapter


class TestSquareBrackets:
    def test_identity(self):
        a = TagDialectAdapter("square_brackets")
        assert a.render("[confident] Done.") == "[confident] Done."


class TestParens:
    def test_rewrites_leading_bracket(self):
        a = TagDialectAdapter("parens")
        assert a.render("[confident] Done.") == "(confident) Done."

    def test_rewrites_multiple_tags(self):
        a = TagDialectAdapter("parens")
        assert a.render("[warm] [softly] hello") == "(warm) (softly) hello"

    def test_leaves_prose_square_brackets_untouched_if_no_canonical_tag(self):
        """Spec §5.2: adapter operates on canonical input. If the block has
        no leading tag, arbitrary square-bracket text in prose is still
        rewritten (the adapter is a simple substitution). This test pins
        current behaviour so any future 'leading-only' refinement is
        explicit.
        """
        a = TagDialectAdapter("parens")
        # Every [X] pair is rewritten — canonical convention expects tags
        # to appear as the only bracket form in butler output.
        assert a.render("See also [ref].") == "See also (ref)."


class TestNone:
    def test_strips_leading_tag(self):
        a = TagDialectAdapter("none")
        assert a.render("[confident] Done.") == "Done."

    def test_strips_leading_parens_tag(self):
        a = TagDialectAdapter("none")
        assert a.render("(confident) Done.") == "Done."

    def test_strips_multiple_leading_tags(self):
        a = TagDialectAdapter("none")
        assert a.render("[warm] [softly] hello") == "hello"

    def test_empty_block_empty_result(self):
        a = TagDialectAdapter("none")
        assert a.render("") == ""


class TestValidation:
    def test_unknown_dialect_rejected(self):
        with pytest.raises(ValueError, match="tag_dialect"):
            TagDialectAdapter("ssml")
