"""#429: ``casa.setupProvides`` / ``casa.optionalEnv`` — the two manifest
declarations that let a plugin say which of its ``.mcp.json`` env references
Casa must not withhold it for.

Both RELAX the env-readiness gate, so both are read STRICTLY on every
artifact-verification path (install-time ``validate_manifest`` and
resolution-time ``artifact_verdict``), exactly like ``casa.setupTool``:
a declaration Casa would have to interpret is refused rather than guessed
at. The gate/session-build behaviour they drive lives in
tests/test_plugin_grants.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import plugin_store
from plugin_store import (
    METADATA_FILENAME,
    StoreError,
    content_checksum,
    manifest_optional_env,
    manifest_setup_provides,
)

pytestmark = pytest.mark.unit


def _manifest(**casa) -> dict:
    return {"name": "p", "version": "1.0.0", "casa": casa}


# ---------------------------------------------------------------------------
# manifest_setup_provides
# ---------------------------------------------------------------------------

def test_setup_provides_absent_is_empty():
    assert manifest_setup_provides({}) == []
    assert manifest_setup_provides({"casa": {}}) == []
    assert manifest_setup_provides({"casa": "not-a-dict"}) == []


def test_setup_provides_returns_declared_names_in_order():
    m = _manifest(setupTool="setup_bank_feed",
                  setupProvides=["CASA_PLUGIN_BANKFEED_PRIVATE_KEY",
                                 "CASA_PLUGIN_BANKFEED_APP_ID"])
    assert manifest_setup_provides(m) == ["CASA_PLUGIN_BANKFEED_PRIVATE_KEY",
                                          "CASA_PLUGIN_BANKFEED_APP_ID"]


def test_setup_provides_without_setup_tool_is_refused():
    """The field means 'my setup tool provisions these'. Without a setupTool
    it would be an undeclared way to mark a credential optional — which is
    what casa.optionalEnv is for, and which carries a different readiness
    meaning on the verify surface."""
    with pytest.raises(StoreError) as exc:
        manifest_setup_provides(_manifest(setupProvides=["CASA_PLUGIN_A_KEY"]))
    assert exc.value.reason_code == "setup_provides_invalid"


@pytest.mark.parametrize("value", [
    "CASA_PLUGIN_A",                      # a bare string, not a list
    {"CASA_PLUGIN_A": True},              # a mapping (its KEYS must not leak through)
    ["lowercase_var"],             # not env-var grammar
    ["9LEADING_DIGIT"],
    ["HAS-HYPHEN"],
    ["WITH SPACE"],
    ["${INTERPOLATED}"],
    [""],
    [None],
    [123],
    ["CASA_PLUGIN_DUPE", "CASA_PLUGIN_DUPE"],
    ["CASA_PLUGIN_" + "V" * 129],   # over the length cap
])
def test_setup_provides_malformed_raises(value):
    with pytest.raises(StoreError) as exc:
        manifest_setup_provides(
            _manifest(setupTool="setup_x", setupProvides=value))
    assert exc.value.reason_code == "setup_provides_invalid"


def test_setup_provides_count_is_capped():
    names = [f"CASA_PLUGIN_VAR_{i}" for i in range(33)]
    with pytest.raises(StoreError) as exc:
        manifest_setup_provides(
            _manifest(setupTool="setup_x", setupProvides=names))
    assert exc.value.reason_code == "setup_provides_invalid"


# ---------------------------------------------------------------------------
# manifest_optional_env
# ---------------------------------------------------------------------------

def test_optional_env_absent_is_empty():
    assert manifest_optional_env({}) == []
    assert manifest_optional_env({"casa": {}}) == []


def test_optional_env_needs_no_setup_tool():
    """Unlike setupProvides, an optional variable is meaningful on a plugin
    with no setup tool at all."""
    assert manifest_optional_env(
        _manifest(optionalEnv=["CASA_PLUGIN_BANKFEED_CP_TOKEN"])) == [
            "CASA_PLUGIN_BANKFEED_CP_TOKEN"]


@pytest.mark.parametrize("value", ["CASA_PLUGIN_A", {"A": 1}, ["lower"], [7],
                                   ["CASA_PLUGIN_D", "CASA_PLUGIN_D"]])
def test_optional_env_malformed_raises(value):
    with pytest.raises(StoreError) as exc:
        manifest_optional_env(_manifest(optionalEnv=value))
    assert exc.value.reason_code == "optional_env_invalid"


# ---------------------------------------------------------------------------
# Both validation paths
# ---------------------------------------------------------------------------

def test_validate_manifest_refuses_a_malformed_declaration(tmp_path):
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(_manifest(setupTool="setup_x", optionalEnv="nope")),
        encoding="utf-8")
    with pytest.raises(StoreError) as exc:
        plugin_store.validate_manifest(root, expected_name="p")
    assert exc.value.reason_code == "optional_env_invalid"


def _published_with_rewritten_manifest(tmp_path, manifest: dict) -> Path:
    """Publish a clean artifact, then rewrite plugin.json in place and
    re-align the stored checksum — the pre-validator-artifact simulation the
    setupTool verdict test uses, so only the new gate can catch the result."""
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    res = plugin_store.publish_from_tree(
        name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=tmp_path / "store",
        staging_root=tmp_path / "staging")
    art = Path(res.path)
    pj = art / ".claude-plugin" / "plugin.json"
    os.chmod(art, 0o755)
    os.chmod(art / ".claude-plugin", 0o755)
    os.chmod(pj, 0o644)
    pj.write_text(json.dumps(manifest), encoding="utf-8")
    meta_path = art / METADATA_FILENAME
    os.chmod(meta_path, 0o644)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["content_checksum"] = content_checksum(art)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True),
                         encoding="utf-8")
    return art, res.artifact_id


def test_artifact_verdict_rechecks_setup_provides(tmp_path):
    """Upgrade path: an artifact published before this release can carry a
    declaration the publish-time gate never saw. It must be EXCLUDED from
    resolution — degrading to 'no declaration' would silently re-impose the
    deadlock, and reading it loosely would relax the gate for a name the
    author never wrote."""
    art, artifact_id = _published_with_rewritten_manifest(
        tmp_path, _manifest(setupTool="setup_x", setupProvides=["lower"]))
    assert plugin_store.artifact_verdict(
        art, name="p", repo="o/r", revision="git:" + "a" * 40,
        subdir="", artifact_id=artifact_id) == "setup_provides_invalid"


def test_artifact_verdict_rechecks_optional_env(tmp_path):
    art, artifact_id = _published_with_rewritten_manifest(
        tmp_path, _manifest(optionalEnv={"A": 1}))
    assert plugin_store.artifact_verdict(
        art, name="p", repo="o/r", revision="git:" + "a" * 40,
        subdir="", artifact_id=artifact_id) == "optional_env_invalid"


def test_artifact_verdict_accepts_a_well_formed_declaration(tmp_path):
    art, artifact_id = _published_with_rewritten_manifest(
        tmp_path, _manifest(setupTool="setup_bank_feed",
                            setupProvides=["CASA_PLUGIN_BANKFEED_APP_ID"],
                            optionalEnv=["CASA_PLUGIN_BANKFEED_CP_TOKEN"]))
    assert plugin_store.artifact_verdict(
        art, name="p", repo="o/r", revision="git:" + "a" * 40,
        subdir="", artifact_id=artifact_id) is None


# ---------------------------------------------------------------------------
# A declared name is BOUND — the session builder pins it to "" while it is
# unresolved, and that binding is process-wide for the CLI subprocess, not
# scoped to the declaring plugin. Review rounds 1 and 2 each answered "may
# this plugin declare this name?" with a deny-list, and a reviewer found a
# miss each time (MCP_TOOL_TIMEOUT, then GIT_DIR). The environment namespace
# is open, so the enumeration was the wrong shape: the answer is a RESERVED
# declaration prefix, which excludes every such name by construction.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "OP_SERVICE_ACCOUNT_TOKEN",   # Casa-owned, from an app option
    "CONTEXT7_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "ANTHROPIC_API_KEY",          # runtime auth — absence is meaningful
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
    "MCP_TOOL_TIMEOUT",           # r2 (Terra): the run template exports it
    "GIT_DIR",                    # r2 (Sol): would break every git call
    "PATH",
    "LD_PRELOAD",
    "MY_PLUGIN_TOKEN",            # plugin-ish, but outside the namespace
    "CASA_VERSION",               # Casa's own CASA_-prefixed export
    "CASA_PLUGIN_",               # the bare prefix is not a name
    "CASA_PLUGIN_lower",
])
def test_only_the_reserved_declaration_namespace_is_declarable(name):
    with pytest.raises(StoreError) as exc:
        manifest_optional_env(_manifest(optionalEnv=[name]))
    assert exc.value.reason_code == "optional_env_invalid"
    with pytest.raises(StoreError):
        manifest_setup_provides(
            _manifest(setupTool="setup_x", setupProvides=[name]))


def test_a_reserved_namespace_name_is_accepted():
    assert manifest_optional_env(
        _manifest(optionalEnv=["CASA_PLUGIN_BANKFEED_CP_TOKEN",
                               "CASA_PLUGIN_X"])) == [
        "CASA_PLUGIN_BANKFEED_CP_TOKEN", "CASA_PLUGIN_X"]


def test_referencing_an_undeclarable_name_is_still_allowed(tmp_path):
    """Only DECLARED names are fenced. A plugin may reference anything in
    .mcp.json — an undeclared reference simply withholds the plugin as
    before, binding nothing."""
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(_manifest()), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": {
        "command": "node",
        "env": {"T": "${OP_SERVICE_ACCOUNT_TOKEN}"}}}}), encoding="utf-8")
    assert plugin_store.validate_manifest(root, expected_name="p") is not None
