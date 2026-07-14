"""§3.2 publish pipeline: ref resolution failure taxonomy, staging + atomic
rename, idempotent re-publish, corrupt-destination fail-closed, manifest
validation, bundle import."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import plugin_store
from plugin_registry import compute_artifact_id
from plugin_store import (
    METADATA_FILENAME,
    RefNotFound,
    ResolveUnavailable,
    StoreError,
    content_checksum,
    import_bundle,
    publish,
    publish_from_tree,
    resolve_ref,
    validate_artifact,
    validate_manifest,
)

pytestmark = pytest.mark.unit

SHA = "a" * 40


def _unfreeze(p: Path) -> None:
    """Restore write on a published artifact file — publish() now freezes files
    read-only (Sol #7). Tests that simulate corruption which BYPASSED the freeze
    (privileged process / disk error) must defeat it first; the artifact_verdict
    backstop must still catch the corruption."""
    import os
    import stat
    os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | 0o200)


def _plugin_tree(tmp_path, name="probe", version="1.0.0") -> Path:
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "s.md").write_text("skill", encoding="utf-8")
    return root


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _gh_response(status: int, body: str, headers: dict | None = None) -> str:
    """Render `gh api -i` stdout: status line + headers + blank + body."""
    hdrs = {"content-type": "application/json; charset=utf-8", **(headers or {})}
    head = "\n".join([f"HTTP/2.0 {status} X"] + [f"{k}: {v}" for k, v in hdrs.items()])
    return f"{head}\n\n{body}"


def _proc_for(status: int, body: str, headers: dict | None = None) -> "_Proc":
    return _Proc(0 if 200 <= status < 300 else 1,
                 _gh_response(status, body, headers), "")


def test_resolve_ref_happy_bare_sha_with_headers():
    """200 via `gh api -i … --jq .sha`: headers block + bare sha body."""
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(200, SHA + "\n")) as run:
        assert resolve_ref("o/r", "v1.0.0") == SHA
    argv = run.call_args[0][0]
    assert argv[:3] == ["gh", "api", "-i"]
    assert argv[3] == "repos/o/r/commits/v1.0.0"
    assert argv[-2:] == ["--jq", ".sha"]


def test_resolve_ref_happy_tolerates_json_body():
    """Belt-and-braces: a full-JSON 200 body still parses (jq not applied)."""
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(200, json.dumps({"sha": SHA}))):
        assert resolve_ref("o/r", "v1.0.0") == SHA


def test_resolve_ref_422_no_commit_is_ref_not_found():
    """THE primary fix: missing tag/sha/branch → 422 'No commit found for
    SHA' → hard ref_not_found, never retryable-unavailable."""
    body = json.dumps({"message": "No commit found for SHA: v9999.9.9",
                       "documentation_url": "https://docs.github.com/rest",
                       "status": "422"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(422, body)):
        with pytest.raises(RefNotFound):
            resolve_ref("o/r", "v9999.9.9")


def test_resolve_ref_422_other_is_resolve_unavailable():
    body = json.dumps({"message": "Validation Failed", "status": "422"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(422, body)):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "weird")


def test_resolve_ref_404_is_ref_not_found():
    body = json.dumps({"message": "Not Found", "status": "404"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(404, body)):
        with pytest.raises(RefNotFound) as ei:
            resolve_ref("o/r", "phantom-tag")
    assert "not visible" in str(ei.value)          # spec wording


def test_resolve_ref_401_is_resolve_auth_failed():
    from plugin_store import ResolveAuthFailed
    body = json.dumps({"message": "Bad credentials", "status": "401"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(401, body)):
        with pytest.raises(ResolveAuthFailed):
            resolve_ref("o/r", "v1.0.0")


def test_resolve_ref_403_not_ratelimited_is_resolve_auth_failed():
    from plugin_store import ResolveAuthFailed
    body = json.dumps({"message": "Resource not accessible by integration",
                       "status": "403"})
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(403, body,
                                      {"x-ratelimit-remaining": "42"})):
        with pytest.raises(ResolveAuthFailed):
            resolve_ref("o/r", "v1.0.0")


def test_resolve_ref_409_empty_repo_is_source_empty():
    from plugin_store import SourceEmpty
    body = json.dumps({"message": "Git Repository is empty.", "status": "409"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(409, body)):
        with pytest.raises(SourceEmpty):
            resolve_ref("o/r", "main")


def test_resolve_ref_409_other_is_resolve_unavailable():
    body = json.dumps({"message": "Conflict", "status": "409"})
    with patch("plugin_store.subprocess.run", return_value=_proc_for(409, body)):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "main")


def test_resolve_ref_5xx_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(503, '{"message": "Service Unavailable"}')):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_no_status_line_is_resolve_unavailable():
    """Tooling failure (no HTTP response on stdout) stays retryable."""
    with patch("plugin_store.subprocess.run",
               return_value=_Proc(1, "", "error connecting to api.github.com")):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_timeout_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run",
               side_effect=subprocess.TimeoutExpired(["gh"], 20)):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_missing_gh_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_normalize_revision():
    from plugin_store import normalize_revision
    assert normalize_revision("git:" + "A" * 40) == "a" * 40
    assert normalize_revision("a" * 40) == "a" * 40
    assert normalize_revision(" git:" + "b" * 40 + " ") == "b" * 40
    assert normalize_revision("g" * 40) is None          # not hex
    assert normalize_revision("abc") is None
    assert normalize_revision(None) is None
    assert normalize_revision(1234) is None


def test_resolve_ref_ratelimit_403_retries_then_succeeds():
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    responses = [
        _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "1"}),
        _proc_for(200, SHA + "\n"),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses):
        assert resolve_ref("o/r", "v1", _sleep=sleeps.append) == SHA
    assert sleeps == [1.0]           # Retry-After honored exactly


def test_resolve_ref_429_retries():
    responses = [
        _proc_for(429, '{"message": "too many requests"}', {"retry-after": "2"}),
        _proc_for(200, SHA + "\n"),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses):
        assert resolve_ref("o/r", "v1", _sleep=sleeps.append) == SHA
    assert sleeps == [2.0]


def test_resolve_ref_ratelimit_exhaustion_bounded_and_carries_metadata():
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    proc = _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "1"})
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", return_value=proc) as run:
        with pytest.raises(ResolveUnavailable) as ei:
            resolve_ref("o/r", "v1", _sleep=sleeps.append)
    assert run.call_count == 3               # <=3 attempts (C.3)
    assert len(sleeps) == 2                  # waits only BETWEEN attempts
    assert ei.value.retry_after_s == 1.0     # latest Retry-After surfaced


def test_resolve_ref_retry_after_exceeding_budget_returns_immediately():
    """A Retry-After above the 60s budget is NEVER waited or truncated:
    immediate ResolveUnavailable carrying the server's requested delay."""
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    proc = _proc_for(403, body, {"x-ratelimit-remaining": "0",
                                 "retry-after": "3600"})
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", return_value=proc) as run:
        with pytest.raises(ResolveUnavailable) as ei:
            resolve_ref("o/r", "v1", _sleep=sleeps.append)
    assert run.call_count == 1 and sleeps == []
    assert ei.value.retry_after_s == 3600.0


def test_resolve_ref_cumulative_budget_never_exceeded():
    """r2-B5: each delay individually < 60s but the SUM would exceed the 60s
    TOTAL budget — sleep only the first (40s), stop before the second, and
    surface the un-waited delay as retry metadata."""
    body = json.dumps({"message": "API rate limit exceeded", "status": "403"})
    responses = [
        _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "40"}),
        _proc_for(403, body, {"x-ratelimit-remaining": "0", "retry-after": "30"}),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses) as run:
        with pytest.raises(ResolveUnavailable) as ei:
            resolve_ref("o/r", "v1", _sleep=sleeps.append)
    assert run.call_count == 2               # 2nd response seen, 3rd never tried
    assert sleeps == [40.0]                  # 40+30 > 60 → second wait refused
    assert ei.value.retry_after_s == 30.0    # the refused delay is surfaced


def test_resolve_ref_secondary_ratelimit_recognized_by_body_only():
    """C.3: headers inconclusive -> body text recognizes a secondary limit."""
    body = json.dumps({"message":
                       "You have exceeded a secondary rate limit. "
                       "Please wait a few minutes before you try again."})
    responses = [
        _proc_for(403, body),        # NO rate-limit headers at all
        _proc_for(200, SHA + "\n"),
    ]
    sleeps: list[float] = []
    with patch("plugin_store.subprocess.run", side_effect=responses):
        assert resolve_ref("o/r", "v1", _sleep=sleeps.append) == SHA
    assert sleeps == [2.0]           # default backoff (no Retry-After)


def test_resolve_ref_non_ratelimit_transient_does_not_retry_in_function():
    """5xx is a retryable VERDICT for the caller, not an in-function loop."""
    with patch("plugin_store.subprocess.run",
               return_value=_proc_for(502, '{"message": "Bad gateway"}')) as run:
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1", _sleep=lambda s: (_ for _ in ()).throw(
                AssertionError("must not sleep")))
    assert run.call_count == 1


def test_publish_with_precommitted_sha_skips_resolve(tmp_path):
    """C.2: the identity guards resolve ONCE; publish(commit=) must not
    re-resolve (a tag moving between resolve and fetch would be a TOCTOU)."""
    import shutil
    src = _plugin_tree(tmp_path)

    def _no_resolve(*a, **k):
        raise AssertionError("resolve_ref must not be called")

    with patch("plugin_store.resolve_ref", side_effect=_no_resolve), \
         patch("plugin_store.fetch_commit_tree",
               side_effect=lambda repo, commit, subdir, dest, **k:
               shutil.copytree(src, dest, dirs_exist_ok=True)) as fct:
        res = publish(name="probe", repo="o/r", ref="v1.0.0",
                      store_root=tmp_path / "store",
                      staging_root=tmp_path / "staging", commit=SHA)
    assert res.revision == f"git:{SHA}"
    assert fct.call_args[0][1] == SHA


def test_validate_manifest_paths(tmp_path):
    root = _plugin_tree(tmp_path)
    assert validate_manifest(root, "probe")["version"] == "1.0.0"
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "other-name")
    assert ei.value.reason_code == "name_mismatch"
    (root / ".claude-plugin" / "plugin.json").write_text("{broken",
                                                         encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "manifest_invalid"


def test_validate_manifest_defaults_missing_version(tmp_path):
    """CI/real-world: plugins like anthropics/claude-plugins-official ship NO
    top-level version. validate_manifest must default it (0.0.0), not reject —
    version is no longer identity-load-bearing. The unit gate's versioned
    fixtures masked this; only the image build caught it."""
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "probe"}), encoding="utf-8")   # no version
    assert validate_manifest(root, "probe")["version"] == "0.0.0"


def test_validate_manifest_tolerates_non_object_casa(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "probe", "version": "1.0.0", "casa": "oops"}),
        encoding="utf-8")
    assert validate_manifest(root, "probe")["version"] == "1.0.0"


def test_validate_manifest_rejects_apt(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"systemRequirements": [{"type": "apt", "package": "x"}]},
    }), encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "apt_requirements_rejected"


# --- A:§3.7 casa.protectedTools (v0.76.0) ------------------------------------


def test_manifest_protected_tools_absent_is_empty():
    from plugin_store import manifest_protected_tools
    assert manifest_protected_tools({"name": "p", "version": "1.0.0"}) == []
    assert manifest_protected_tools({"name": "p", "casa": {}}) == []
    # Sol R2-3-style tolerance: a non-object casa degrades to [], not a raise.
    assert manifest_protected_tools({"name": "p", "casa": "oops"}) == []
    assert manifest_protected_tools({}) == []


def test_manifest_protected_tools_present_valid():
    """Legacy string form normalizes to {"name", "summary": None}."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": ["invoice_reset"]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "invoice_reset", "summary": None}]


def test_manifest_protected_tools_present_empty_list_is_valid():
    """The key PRESENT but an empty list is a valid (vacuous) list of
    non-empty strings — not malformed."""
    from plugin_store import manifest_protected_tools
    assert manifest_protected_tools(
        {"name": "p", "casa": {"protectedTools": []}}) == []


@pytest.mark.parametrize("bad_value", [
    "invoice_reset",                 # str, not a list
    ["invoice_reset", ""],           # list-with-empty-string
    ["invoice_reset", 1],            # list-with-int
    {"invoice_reset": True},         # dict, not a list
])
def test_manifest_protected_tools_malformed_shapes_raise(bad_value):
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": bad_value}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


# --- v0.78.0 W1: object-form entries with summaries --------------------------


def test_manifest_protected_tools_object_entry_normalization():
    """An object entry with a summary normalizes to {"name", "summary"}."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "invoice_reset", "summary": "Delete the draft for {period}"},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "invoice_reset", "summary": "Delete the draft for {period}"}]


def test_manifest_protected_tools_object_entry_without_summary():
    """Object form WITHOUT a summary key normalizes summary to None, same as
    the legacy string form."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "invoice_reset"},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "invoice_reset", "summary": None}]


def test_manifest_protected_tools_mixed_string_and_object_entries():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        "legacy_tool",
        {"name": "new_tool", "summary": "Does the new thing"},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "legacy_tool", "summary": None},
        {"name": "new_tool", "summary": "Does the new thing"},
    ]


@pytest.mark.parametrize("bad_entry", [
    123,                                          # wrong type (not str/dict)
    None,                                         # wrong type
    [],                                           # wrong type (list)
    {"summary": "no name"},                       # missing name
    {"name": ""},                                 # empty name
    {"name": "t", "summary": ""},                 # empty summary
    {"name": "t", "summary": 5},                  # non-string summary
    {"name": "t", "summary": "x" * 201},          # oversized summary (>200)
    {"name": "t", "summary": "line one\nline two"},   # multiline (C0 \n)
    {"name": "t", "summary": "bad\x01char"},           # C0 control
    {"name": "t", "summary": "bad" + chr(0x9c) + "char"},  # C1 control
    {"name": "t", "summary": "line" + chr(0x2028) + "sep"},  # U+2028
    {"name": "t", "summary": "bidi" + chr(0x200e) + "mark"},  # U+200E LRM
    {"name": "t", "summary": "bidi" + chr(0x202a) + "embed"},  # U+202A
    {"name": "t", "unknown_key": "x"},            # unknown key
    {"name": "t", "summary": "ok", "extra": 1},   # unknown key alongside valid
])
def test_manifest_protected_tools_object_shapes_refuse(bad_entry):
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [bad_entry]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_object_summary_exactly_200_chars_ok():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "t", "summary": "x" * 200},
    ]}}
    assert manifest_protected_tools(manifest) == [
        {"name": "t", "summary": "x" * 200}]


# --- v0.78.0 W1: duplicate names after sanitize_segment ----------------------


def test_manifest_protected_tools_duplicate_string_string_refuses():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": ["a_tool", "a_tool"]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_duplicate_object_object_refuses():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        {"name": "a_tool", "summary": "first"},
        {"name": "a_tool", "summary": "second"},
    ]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_duplicate_mixed_string_object_refuses():
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        "a_tool",
        {"name": "a_tool", "summary": "obj form"},
    ]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_manifest_protected_tools_sanitized_collision_duplicate_refuses():
    """r3-1: 'do thing' and 'do_thing' sanitize to the same runtime tool id —
    no order-dependent last-wins summary semantics."""
    from plugin_store import manifest_protected_tools
    manifest = {"name": "p", "casa": {"protectedTools": [
        "do thing", "do_thing",
    ]}}
    with pytest.raises(StoreError) as ei:
        manifest_protected_tools(manifest)
    assert ei.value.reason_code == "protected_tools_invalid"


def test_validate_manifest_accepts_absent_protected_tools(tmp_path):
    root = _plugin_tree(tmp_path)
    assert validate_manifest(root, "probe")["version"] == "1.0.0"


def test_validate_manifest_accepts_valid_protected_tools(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"protectedTools": ["invoice_reset"]},
    }), encoding="utf-8")
    assert validate_manifest(root, "probe")["version"] == "1.0.0"


@pytest.mark.parametrize("bad_value", [
    "invoice_reset", ["invoice_reset", ""], ["invoice_reset", 1],
    {"invoice_reset": True},
])
def test_validate_manifest_rejects_malformed_protected_tools(tmp_path, bad_value):
    """A PRESENT but malformed casa.protectedTools FAILS validate_manifest —
    install/update refused (A:§3.7 B7 strict validator)."""
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"protectedTools": bad_value},
    }), encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "protected_tools_invalid"


def test_artifact_verdict_flags_stored_malformed_protected_tools(tmp_path):
    """A:§3.7 (r2-B6/r3-4): a PRE-EXISTING stored artifact whose manifest is
    re-checked by artifact_verdict() (the shared strict extension runs
    inside deep validation too) yields protected_tools_invalid — excluding
    it from resolution, never a whole-role failure."""
    from plugin_store import artifact_verdict, content_checksum, write_metadata
    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"protectedTools": ["invoice_reset", 1]},   # malformed
    }), encoding="utf-8")
    write_metadata(root, name="probe", repo="o/r", ref="v1",
                   revision="git:" + SHA, subdir="", artifact_id="a" * 64,
                   version="1.0.0", checksum=content_checksum(root))
    verdict = artifact_verdict(root, name="probe", repo="o/r",
                               revision="git:" + SHA, subdir="",
                               artifact_id="a" * 64)
    assert verdict == "protected_tools_invalid"


def test_artifact_verdict_absent_protected_tools_is_valid(tmp_path):
    from plugin_store import artifact_verdict, content_checksum, write_metadata
    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "probe", "version": "1.0.0"}), encoding="utf-8")
    write_metadata(root, name="probe", repo="o/r", ref="v1",
                   revision="git:" + SHA, subdir="", artifact_id="a" * 64,
                   version="1.0.0", checksum=content_checksum(root))
    verdict = artifact_verdict(root, name="probe", repo="o/r",
                               revision="git:" + SHA, subdir="",
                               artifact_id="a" * 64)
    assert verdict is None


def _wire_fetch(src_root):
    """publish() fetches into staging: fake fetch_commit_tree by copying."""
    import shutil

    def _fake(repo, commit, subdir, dest, **kw):
        shutil.copytree(src_root, dest, dirs_exist_ok=True, symlinks=True)
    return _fake


def test_publish_happy_atomic(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        res = publish(name="probe", repo="o/r", ref="v1",
                      store_root=store, staging_root=staging)
    assert res.revision == f"git:{SHA}"
    assert res.version == "1.0.0"
    dest = store / "probe" / res.artifact_id
    assert Path(res.path) == dest and validate_artifact(dest)
    assert not any(staging.iterdir())          # staging cleaned


def test_publish_existing_valid_is_noop(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        r2 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
    assert r1.artifact_id == r2.artifact_id


def test_publish_existing_corrupt_fails_closed(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        # Tamper the published artifact (defeat the Sol #7 freeze to model
        # corruption that bypassed it — the verdict backstop must still catch it).
        _unfreeze(Path(r1.path) / "skills" / "s.md")
        (Path(r1.path) / "skills" / "s.md").write_text("evil", encoding="utf-8")
        with pytest.raises(StoreError) as ei:
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert ei.value.reason_code == "corrupt_artifact"
    # Nothing swapped: tampered content still in place (operator/GC recovers).
    assert (Path(r1.path) / "skills" / "s.md").read_text(
        encoding="utf-8") == "evil"


def test_publish_existing_wrong_identity_metadata_fails_closed(tmp_path):
    """A destination whose checksum self-validates but whose metadata names a
    DIFFERENT identity is corrupt — never silently accepted."""
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        meta_path = Path(r1.path) / METADATA_FILENAME
        _unfreeze(meta_path)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["revision"] = "git:" + "b" * 40      # wrong identity
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        # re-fix the content checksum so ONLY identity is wrong
        meta["content_checksum"] = content_checksum(Path(r1.path))
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(StoreError) as ei:
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert ei.value.reason_code == "corrupt_artifact"


def test_publish_failure_cleans_staging_store_unchanged(tmp_path):
    src = _plugin_tree(tmp_path, name="WRONG")  # name mismatch → validate fails
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        with pytest.raises(StoreError):
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert not (store / "probe").exists()
    assert not staging.exists() or not any(staging.iterdir())


def test_publish_from_tree_excludes_git_and_uses_given_revision(tmp_path):
    src = _plugin_tree(tmp_path)
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
    store, staging = tmp_path / "store", tmp_path / "staging"
    rev = "legacy-content:" + "c" * 64
    res = publish_from_tree(name="probe", repo="o/r", ref="master",
                            revision=rev, subdir="", src_root=src,
                            store_root=store, staging_root=staging)
    assert res.revision == rev
    assert not (Path(res.path) / ".git").exists()
    expected = compute_artifact_id(repo="o/r", revision=rev, subdir="",
                                   name="probe")
    assert res.artifact_id == expected


def test_import_bundle_idempotent_and_fail_closed(tmp_path):
    src = _plugin_tree(tmp_path)
    bundle, store = tmp_path / "bundle", tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=bundle, staging_root=tmp_path / "stg")
    issues = import_bundle(bundle, store_root=store)
    assert issues == []
    dest = store / "probe" / res.artifact_id
    assert validate_artifact(dest)
    assert import_bundle(bundle, store_root=store) == []   # idempotent
    # Corrupt the store copy → issue raised, NOT silently replaced.
    _unfreeze(dest / "skills" / "s.md")
    (dest / "skills" / "s.md").write_text("evil", encoding="utf-8")
    issues = import_bundle(bundle, store_root=store)
    assert [i.reason_code for i in issues] == ["corrupt_artifact"]


def test_publish_freezes_artifact_files_readonly(tmp_path):
    """Sol #7: a published artifact's files are read-only (no write bit for any
    class) so in-place tampering can't defeat the cached deep-validation."""
    import os
    import stat
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r = publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    skill = Path(r.path) / "skills" / "s.md"
    mode = stat.S_IMODE(os.lstat(skill).st_mode)
    assert mode & 0o222 == 0, f"artifact file still writable: {oct(mode)}"
    # verify_bin backstop still readable (deep validation must pass).
    from plugin_store import validate_artifact
    assert validate_artifact(Path(r.path))


def test_gc_disabled_returns_candidates_without_deleting(tmp_path):
    src = _plugin_tree(tmp_path)
    store = tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=store, staging_root=tmp_path / "stg")
    cands = plugin_store.gc_sweep(store_root=store, referenced=set(),
                                  min_age_days=0, enabled=False)
    assert cands == [res.artifact_id] and Path(res.path).exists()


def test_publish_from_tree_rejects_escaping_symlink(tmp_path):
    """Sol round-3 H7: an offline-adopt tree with a symlink escaping the artifact
    root is rejected (unsafe_archive) — freezing/loading it must never touch or
    expose an external file."""
    import os
    src = _plugin_tree(tmp_path)
    os.symlink("/etc/passwd", src / "evil-link")      # escaping absolute symlink
    store, staging = tmp_path / "store", tmp_path / "staging"
    with pytest.raises(StoreError) as ei:
        publish_from_tree(name="probe", repo="o/r", ref="master",
                          revision="legacy-content:" + "c" * 64, subdir="",
                          src_root=src, store_root=store, staging_root=staging)
    assert ei.value.reason_code == "unsafe_archive"
    assert not (store / "probe").exists()             # nothing published


def test_publish_from_tree_allows_internal_symlink(tmp_path):
    """Sol round-3 H7: an in-artifact symlink (non-escaping) is allowed; freeze
    skips it without chmod-following."""
    import os
    src = _plugin_tree(tmp_path)
    (src / "skills" / "target.md").write_text("t", encoding="utf-8")
    os.symlink("target.md", src / "skills" / "link.md")   # internal, relative
    store, staging = tmp_path / "store", tmp_path / "staging"
    res = publish_from_tree(name="probe", repo="o/r", ref="master",
                            revision="legacy-content:" + "c" * 64, subdir="",
                            src_root=src, store_root=store, staging_root=staging)
    assert (Path(res.path) / "skills" / "link.md").is_symlink()  # preserved


def test_import_bundle_freezes_files(tmp_path):
    """Sol round-3 H7: imported bundle artifacts are frozen read-only too."""
    import os
    import stat
    src = _plugin_tree(tmp_path)
    bundle, store = tmp_path / "bundle", tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=bundle, staging_root=tmp_path / "stg")
    import_bundle(bundle, store_root=store)
    skill = store / "probe" / res.artifact_id / "skills" / "s.md"
    assert stat.S_IMODE(os.lstat(skill).st_mode) & 0o222 == 0


def test_publish_rejects_cyclic_symlink(tmp_path):
    """Sol round-4: a symlink LOOP raises unsafe_archive (RuntimeError from
    resolve() translated), not an uncaught error."""
    import os
    src = _plugin_tree(tmp_path)
    os.symlink("b", src / "a")           # a -> b
    os.symlink("a", src / "b")           # b -> a  (cycle)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with pytest.raises(StoreError) as ei:
        publish_from_tree(name="probe", repo="o/r", ref="master",
                          revision="legacy-content:" + "c" * 64, subdir="",
                          src_root=src, store_root=store, staging_root=staging)
    assert ei.value.reason_code == "unsafe_archive"
