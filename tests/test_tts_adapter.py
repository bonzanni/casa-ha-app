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

    def test_keeps_substantive_leading_parenthetical(self):
        """#357: only canonical tag atoms are stripped. A leading
        parenthetical carrying real prose (spaces plus sentence
        punctuation — nothing a prosody tag ever contains) is content,
        not markup, and deleting it can drop safety-relevant speech.
        """
        a = TagDialectAdapter("none")
        block = "(Important: the oven is still on.) Turn it off."
        assert a.render(block) == block

    def test_keeps_capitalized_leading_parenthetical(self):
        # Canonical tags are lowercase ([confident], [flat]); a
        # capitalized parenthetical is prose.
        a = TagDialectAdapter("none")
        assert a.render("(Important) Turn it off.") == (
            "(Important) Turn it off."
        )

    def test_strips_tag_then_keeps_prose_parenthetical(self):
        a = TagDialectAdapter("none")
        assert a.render("[flat] (Warning: gas leak.) Leave now.") == (
            "(Warning: gas leak.) Leave now."
        )


class TestValidation:
    def test_unknown_dialect_rejected(self):
        with pytest.raises(ValueError, match="tag_dialect"):
            TagDialectAdapter("ssml")
