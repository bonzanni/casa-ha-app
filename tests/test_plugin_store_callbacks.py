"""casa.callbacks manifest extractor + bundle-inspect gate.

`plugin_store.manifest_callbacks` mirrors `manifest_triggers`'s
shape: a thin, strict wrapper over `plugin_callbacks.parse_and_validate`
that raises `StoreError(reason_code="callbacks_invalid")` on any intrinsic-
validation error, and is wired into the same two call sites
(`validate_manifest` refuses install/update; `artifact_verdict` degrades an
already-published artifact out of resolution). Unlike triggers, a sourced/
bundled plugin dependency MAY declare casa.callbacks — see
tests/test_specialist_install.py for the bundle-inspect regression pin and
the scoped-name length gate.
"""
from __future__ import annotations

import json

import pytest

from plugin_store import StoreError, manifest_callbacks, validate_manifest

pytestmark = pytest.mark.unit


def _m(callbacks):
    return {"casa": {"callbacks": callbacks}}


# --- extractor: happy path ---------------------------------------------


def test_valid_callback_block_passes():
    callbacks = manifest_callbacks(_m([{"name": "oauth-return"}]), "elevenlabs")
    assert callbacks == [{
        "declared": "oauth-return",
        "effective": "plg-elevenlabs--oauth-return",
    }]


def test_absent_callbacks_is_empty_not_error():
    assert manifest_callbacks({"casa": {}}, "p") == []
    assert manifest_callbacks({}, "p") == []
    assert manifest_callbacks({"casa": "nonsense"}, "p") == []


# --- extractor: malformed block raises callbacks_invalid -----------------


def test_non_list_callbacks_raises_callbacks_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_callbacks(_m({"name": "x"}), "p")
    assert exc.value.reason_code == "callbacks_invalid"


def test_bad_entry_raises_callbacks_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_callbacks(_m(["not-a-dict"]), "p")
    assert exc.value.reason_code == "callbacks_invalid"


def test_bad_name_charset_raises_callbacks_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_callbacks(_m([{"name": "has space"}]), "p")
    assert exc.value.reason_code == "callbacks_invalid"


# --- validate_manifest wiring ---------------------------------------------


def _tree_with_casa(tmp_path, name, casa):
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "casa": casa}),
        encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "s.md").write_text("s", encoding="utf-8")
    return root


def test_validate_manifest_accepts_valid_callbacks(tmp_path):
    root = _tree_with_casa(tmp_path, "elevenlabs", {"callbacks": [
        {"name": "oauth-return"}]})
    mf = validate_manifest(root, "elevenlabs")  # no raise
    assert mf["name"] == "elevenlabs"


def test_validate_manifest_refuses_malformed_callbacks(tmp_path):
    root = _tree_with_casa(tmp_path, "p", {"callbacks": [
        {"name": "plg-reserved-prefix"}]})
    with pytest.raises(StoreError) as exc:
        validate_manifest(root, "p")
    assert exc.value.reason_code == "callbacks_invalid"


def test_absent_callbacks_ok(tmp_path):
    root = _tree_with_casa(tmp_path, "p", {})
    validate_manifest(root, "p")  # no raise


def test_manifest_callbacks_helper_uses_plugin_name():
    manifest = {"casa": {"callbacks": [{"name": "cb"}]}}
    callbacks = manifest_callbacks(manifest, "el")
    assert callbacks[0]["effective"] == "plg-el--cb"


# --- artifact_verdict wiring (upgrade-path posture, mirrors triggers) ----


def test_artifact_verdict_degrades_malformed_callbacks(tmp_path):
    import plugin_store

    root = tmp_path / "artifact"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": "p", "version": "1.0.0",
            "casa": {"callbacks": [{"name": "plg-reserved"}]},
        }), encoding="utf-8")
    (root / "server").mkdir()
    (root / "server" / "server.py").write_text("print('x')\n", encoding="utf-8")
    checksum = plugin_store.content_checksum(root)
    plugin_store.write_metadata(
        root, name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", artifact_id="a" * 64, version="1.0.0", checksum=checksum)

    verdict = plugin_store.artifact_verdict(
        root, name="p", repo="o/r", revision="git:" + "a" * 40, subdir="",
        artifact_id="a" * 64)
    assert verdict == "callbacks_invalid"


def test_artifact_verdict_accepts_valid_callbacks(tmp_path):
    import plugin_store

    root = tmp_path / "artifact"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": "p", "version": "1.0.0",
            "casa": {"callbacks": [{"name": "oauth-return"}]},
        }), encoding="utf-8")
    (root / "server").mkdir()
    (root / "server" / "server.py").write_text("print('x')\n", encoding="utf-8")
    checksum = plugin_store.content_checksum(root)
    plugin_store.write_metadata(
        root, name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", artifact_id="a" * 64, version="1.0.0", checksum=checksum)

    verdict = plugin_store.artifact_verdict(
        root, name="p", repo="o/r", revision="git:" + "a" * 40, subdir="",
        artifact_id="a" * 64)
    assert verdict is None
