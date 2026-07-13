"""A.2 completion guard: every casa_plugin_repo artifact must carry a
verified release identity — annotated vX.Y.Z tag, peel==revision,
ref == 'v' + REMOTE plugin.json version, artifact version == remote."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import plugin_completion_guard as guard
import plugin_store

pytestmark = pytest.mark.unit

SHA = "a" * 40


def _art(**over):
    base = {"kind": "casa_plugin_repo",
            "repo_url": "https://github.com/u/casa-plugin-x.git",
            "plugin_name": "x", "ref": "v1.3.0", "revision": SHA,
            "version": "1.3.0", "visibility": "private"}
    base.update(over)
    return base


def _happy_probe(path, *, timeout=20.0, accept=None, jq=None):
    if path.startswith("repos/u/casa-plugin-x/git/ref/tags/"):
        return 200, {}, json.dumps({"object": {"type": "tag", "sha": "t" * 40}})
    if path.startswith("repos/u/casa-plugin-x/contents/"):
        assert accept == "application/vnd.github.raw+json"
        assert f"ref={SHA}" in path
        return 200, {}, json.dumps({"name": "x", "version": "1.3.0"})
    raise AssertionError(path)


def test_repo_from_url_forms():
    f = guard.repo_from_url
    assert f("https://github.com/u/r.git") == "u/r"
    assert f("https://github.com/u/r") == "u/r"
    assert f("https://github.com/u/r/") == "u/r"
    assert f("git@github.com:u/r.git") == "u/r"
    assert f("u/r") == "u/r"
    assert f("") is None
    assert f(None) is None
    assert f("https://gitlab.com/u/r") is None


def test_valid_artifact_passes():
    with patch.object(plugin_store, "gh_api_probe", side_effect=_happy_probe), \
         patch.object(plugin_store, "resolve_ref", return_value=SHA):
        assert guard.validate_completion_artifacts([_art()]) == []


def test_no_casa_plugin_repo_artifact_rejected():
    for arts in ([{"kind": "other"}], [], ["a-bare-string"]):
        fails = guard.validate_completion_artifacts(arts)
        assert fails[0]["reason_code"] == "no_plugin_repo_artifact"


def test_non_release_ref_rejected():
    for bad in ("main", "master", "1.3.0", "v1.3", "a" * 40, None):
        fails = guard.validate_completion_artifacts([_art(ref=bad)])
        assert fails[0]["reason_code"] == "ref_not_release_tag", bad


def test_lightweight_tag_rejected():
    def probe(path, *, timeout=20.0, accept=None, jq=None):
        return 200, {}, json.dumps({"object": {"type": "commit", "sha": SHA}})
    with patch.object(plugin_store, "gh_api_probe", side_effect=probe):
        fails = guard.validate_completion_artifacts([_art()])
    assert fails[0]["reason_code"] == "tag_not_annotated"


def test_missing_tag_ref_rejected():
    def probe(path, *, timeout=20.0, accept=None, jq=None):
        return 404, {}, json.dumps({"message": "Not Found"})
    with patch.object(plugin_store, "gh_api_probe", side_effect=probe):
        fails = guard.validate_completion_artifacts([_art()])
    assert fails[0]["reason_code"] == "tag_missing"


def test_annotated_tag_that_fails_to_peel_rejected():
    """r2-B5/§4: the tag-ref object exists (annotated) but the commits API
    cannot resolve it — its own branch, distinct from missing-tag and
    sha-mismatch."""
    with patch.object(plugin_store, "gh_api_probe", side_effect=_happy_probe), \
         patch.object(plugin_store, "resolve_ref",
                      side_effect=plugin_store.RefNotFound("no peel")):
        fails = guard.validate_completion_artifacts([_art()])
    assert fails[0]["reason_code"] == "tag_missing"
    assert "does not resolve" in fails[0]["message"]


def test_revision_mismatch_rejected():
    with patch.object(plugin_store, "gh_api_probe", side_effect=_happy_probe), \
         patch.object(plugin_store, "resolve_ref", return_value="b" * 40):
        fails = guard.validate_completion_artifacts([_art()])
    assert fails[0]["reason_code"] == "revision_mismatch"


def test_bad_revision_rejected():
    fails = guard.validate_completion_artifacts([_art(revision="nope")])
    assert fails[0]["reason_code"] == "bad_revision"


def test_remote_manifest_version_mismatch_rejected():
    """The producer can't self-certify: ref vs the REMOTE plugin.json."""
    def probe(path, *, timeout=20.0, accept=None, jq=None):
        if "/git/ref/tags/" in path:
            return 200, {}, json.dumps({"object": {"type": "tag"}})
        return 200, {}, json.dumps({"name": "x", "version": "1.4.0"})
    with patch.object(plugin_store, "gh_api_probe", side_effect=probe), \
         patch.object(plugin_store, "resolve_ref", return_value=SHA):
        fails = guard.validate_completion_artifacts([_art()])
    assert fails[0]["reason_code"] == "tag_version_mismatch"


def test_artifact_version_field_must_match_remote():
    """r2-B5: the completion's own `version` field is enforced too — a
    missing or false version cannot ride through on a correct tag."""
    with patch.object(plugin_store, "gh_api_probe", side_effect=_happy_probe), \
         patch.object(plugin_store, "resolve_ref", return_value=SHA):
        fails = guard.validate_completion_artifacts([_art(version="9.9.9")])
        assert fails[0]["reason_code"] == "version_mismatch"
        fails = guard.validate_completion_artifacts([_art(version=None)])
        assert fails[0]["reason_code"] == "version_mismatch"


def test_every_artifact_validated_not_just_first():
    good, bad = _art(), _art(ref="main")
    with patch.object(plugin_store, "gh_api_probe", side_effect=_happy_probe), \
         patch.object(plugin_store, "resolve_ref", return_value=SHA):
        fails = guard.validate_completion_artifacts([good, bad])
    assert len(fails) == 1 and fails[0]["index"] == 1


def test_transient_gh_failure_is_retryable_rejection():
    with patch.object(plugin_store, "gh_api_probe",
                      side_effect=plugin_store.ResolveUnavailable("rate")):
        fails = guard.validate_completion_artifacts([_art()])
    assert fails[0]["reason_code"] == "resolve_unavailable"


def test_bad_repo_url_rejected():
    fails = guard.validate_completion_artifacts(
        [_art(repo_url="https://evil.example/u/r")])
    assert fails[0]["reason_code"] == "bad_repo_url"
