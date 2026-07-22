import pytest

from canonical_bytes import reject_forbidden_markers


def test_accepts_plain_text() -> None:
    reject_forbidden_markers("Just doctrine prose.\n")  # does not raise


@pytest.mark.parametrize("marker", [
    "${SECRET}", "{{template}}", "{% if x %}", "{# comment #}", "!include other.md",
    "<platform_frame>", "<role_identity>", "<persona>", "<role_doctrine>", "<safety_kernel>",
    "<html>",
])
def test_rejects_every_forbidden_marker(marker: str) -> None:
    with pytest.raises(ValueError, match="template, include, HTML, or delimiter"):
        reject_forbidden_markers(f"Some text {marker} more text.")


def test_case_insensitive() -> None:
    with pytest.raises(ValueError):
        reject_forbidden_markers("<HTML>")
