"""Task 1: `source` on plugin/implementation dependencies (spec §1)."""
import json
import shutil
from pathlib import Path

import pytest

from specialist_component import load_specialist_component

try:
    from tests.specialist_fixtures import write_minimal_component  # see Step 1b
except ImportError:
    from specialist_fixtures import write_minimal_component

SHA = "sha256:" + "0" * 64


def _dep(identifier="mtg", source=None, kind="plugin/implementation"):
    d = {"kind": kind, "identifier": identifier, "digest": SHA}
    if source is not None:
        d["source"] = source
    return d


def _write(tmp_path, extra_deps):
    return write_minimal_component(tmp_path, extra_dependencies=extra_deps)


def test_bundled_source_parses(tmp_path):
    cdir, mpath = _write(tmp_path, [_dep(source={"type": "bundled", "path": "plugins/mtg"})])
    comp = load_specialist_component(cdir, mpath)
    dep = [d for d in comp.dependencies if d.kind == "plugin/implementation"][0]
    assert dep.source is not None and dep.source.type == "bundled"
    assert dep.source.path == "plugins/mtg"


def test_github_source_parses(tmp_path):
    cdir, mpath = _write(tmp_path, [_dep(source={
        "type": "github", "repo": "bonzanni/casa-plugin-gmail",
        "ref": "v0.4.1", "revision": "git:" + "a" * 40})])
    comp = load_specialist_component(cdir, mpath)
    dep = [d for d in comp.dependencies if d.kind == "plugin/implementation"][0]
    assert dep.source.repo == "bonzanni/casa-plugin-gmail"
    assert dep.source.revision == "git:" + "a" * 40


def test_sourceless_dep_still_valid(tmp_path):
    cdir, mpath = _write(tmp_path, [_dep()])
    comp = load_specialist_component(cdir, mpath)
    dep = [d for d in comp.dependencies if d.kind == "plugin/implementation"][0]
    assert dep.source is None


@pytest.mark.parametrize("bad", [
    {"type": "bundled"},                                # missing path
    {"type": "bundled", "path": "/abs/path"},           # absolute
    {"type": "bundled", "path": "../escape"},           # traversal
    {"type": "bundled", "path": "a/../b"},              # embedded traversal
    {"type": "github", "repo": "x/y", "ref": "v1"},     # missing revision
    {"type": "github", "repo": "no-slash", "ref": "v1", "revision": "git:" + "a"*40},
    {"type": "nonsense", "path": "p"},                  # unknown type
])
def test_malformed_source_rejected(tmp_path, bad):
    cdir, mpath = _write(tmp_path, [_dep(source=bad)])
    with pytest.raises(ValueError):
        load_specialist_component(cdir, mpath)


@pytest.mark.parametrize("ident", ["UPPER", "has.dot", "-lead", "x" * 41])
def test_bad_identifier_grammar_rejected_for_sourced(tmp_path, ident):
    cdir, mpath = _write(tmp_path, [_dep(identifier=ident,
                                         source={"type": "bundled", "path": "plugins/x"})])
    with pytest.raises(ValueError):
        load_specialist_component(cdir, mpath)


def test_duplicate_sourced_identifier_rejected(tmp_path):
    cdir, mpath = _write(tmp_path, [
        _dep(source={"type": "bundled", "path": "plugins/mtg"}),
        _dep(source={"type": "github", "repo": "x/y", "ref": "v1",
                     "revision": "git:" + "a" * 40}),
    ])
    with pytest.raises(ValueError, match="duplicate"):
        load_specialist_component(cdir, mpath)


def test_duplicate_kind_identifier_rejected_all_kinds(tmp_path):
    # Sourceless plugin rows with the same (kind, identifier) collide too.
    cdir, mpath = _write(tmp_path, [_dep(), _dep()])
    with pytest.raises(ValueError, match="duplicate"):
        load_specialist_component(cdir, mpath)


def test_duplicate_corpus_data_rejected(tmp_path):
    cdir, mpath = _write(tmp_path, [
        _dep(identifier="corpus-x", kind="corpus/data"),
        _dep(identifier="corpus-x", kind="corpus/data"),
    ])
    with pytest.raises(ValueError, match="duplicate"):
        load_specialist_component(cdir, mpath)
