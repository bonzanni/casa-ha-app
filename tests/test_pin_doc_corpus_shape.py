"""Pinning tests for the corpus's own contract invariants (docs corpus).

These pin observable properties of the tracked docs/ tree itself: the parts of
the doctrine and doc-contract rules a test can check mechanically. Each test
names the invariant it pins and records the demonstrated red case.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

CODE_WINS = (
    "> Code is the source of truth. This file is a map; when it and the code "
    "disagree, the code wins."
)

SECTION_ORDER = [
    "## Scope", "## Mental model", "## Contracts & invariants",
    "## Failure behavior", "## Extension points", "## Source & test map",
]


def _documents():
    entries = yaml.safe_load((DOCS / "manifest.yaml").read_text())
    return [
        e["doc"] for e in entries
        if e.get("kind", "document") == "document"
    ]


def _body_without_front_matter(text):
    if text.startswith("---\n"):
        end = text.index("\n---\n", 4)
        return text[end + 5:]
    return text


def test_pin_inv_doc_001_front_matter_and_code_wins_line():
    """INV-DOC-001: every document repeats the code-wins line verbatim at the
    top, under front matter carrying last_reviewed.

    Red case demonstrated: removing the code-wins line from one document
    fails this test.
    """
    for doc in _documents():
        text = (DOCS / doc).read_text()
        assert text.startswith("---\n"), doc
        front = text[4:text.index("\n---\n", 4)]
        assert "last_reviewed:" in front, doc
        assert CODE_WINS in text, doc


def test_pin_inv_doc_002_architecture_documents_follow_the_section_order():
    """INV-DOC-002: an architecture document follows the one section order.

    Red case demonstrated: swapping two section headings in one architecture
    document fails this test.
    """
    for doc in _documents():
        if not doc.startswith("architecture/"):
            continue
        text = (DOCS / doc).read_text()
        positions = [text.index(h) for h in SECTION_ORDER]
        assert positions == sorted(positions), doc


def test_pin_inv_doc_003_no_changelog_voice_markers():
    """INV-DOC-003: a document describes the present system — no TODOs, no
    "as of version" changelog voice. (The tense rule itself is a reviewer
    obligation; these are its mechanical markers.)

    Red case demonstrated: adding a TODO to one document fails this test.
    """
    for doc in _documents():
        body = _body_without_front_matter((DOCS / doc).read_text())
        # The rule's own definition line quotes the banned markers; exempt it.
        lines = [l for l in body.splitlines() if "INV-DOC-003" not in l]
        body = "\n".join(lines)
        assert not re.search(r"\bTODO\b", body), doc
        assert not re.search(r"as of version \d", body, re.IGNORECASE), doc


def test_pin_inv_pub_002_doctrine_carries_no_dated_history():
    """INV-PUB-002: doctrine states the mechanism, never the incident. The
    mechanical marker a test can pin: outside front matter, no doctrine
    document contains an ISO date — dates are incident-history shaped.

    Red case demonstrated: adding a dated incident sentence to a doctrine
    document fails this test.
    """
    for doc in _documents():
        if not doc.startswith("doctrine/"):
            continue
        body = _body_without_front_matter((DOCS / doc).read_text())
        assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", body), doc
