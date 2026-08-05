"""casa.emits / casa.subscribes manifest extractors + validate_manifest /
artifact_verdict wiring.

`plugin_store.manifest_emits`/`manifest_subscribes` mirror `manifest_callbacks`'s
shape: thin, strict wrappers over `plugin_events.parse_and_validate_emits`/
`parse_and_validate_subscribes` that raise `StoreError(reason_code=
"emits_invalid")`/`StoreError(reason_code="subscribes_invalid")` on any
intrinsic-validation error, and are wired into the same two call sites
(`validate_manifest` refuses install/update; `artifact_verdict` degrades an
already-published artifact out of resolution). See
tests/test_specialist_install.py for the bundle-inspect scoped-name-length
gate (`event_name_too_long`) and the `casa.triggers`-still-refused pin.
"""
from __future__ import annotations

import json

import pytest

from plugin_store import StoreError, manifest_emits, manifest_subscribes, validate_manifest

pytestmark = pytest.mark.unit


def _m_emits(emits):
    return {"casa": {"emits": emits}}


def _m_subscribes(subscribes):
    return {"casa": {"subscribes": subscribes}}


# --- extractor: happy path -------------------------------------------------


def test_valid_emits_block_passes():
    emits = manifest_emits(_m_emits([{"name": "invoice-ready"}]), "billing")
    assert emits == [{
        "declared": "invoice-ready",
        "effective": "plg-billing--invoice-ready",
    }]


def test_valid_subscribes_block_passes():
    subs = manifest_subscribes(
        _m_subscribes([{"plugin": "billing", "event": "invoice-ready"}]), "notifier")
    assert len(subs) == 1
    assert subs[0]["plugin"] == "billing"
    assert subs[0]["event"] == "invoice-ready"


def test_absent_emits_is_empty_not_error():
    assert manifest_emits({"casa": {}}, "p") == []
    assert manifest_emits({}, "p") == []
    assert manifest_emits({"casa": "nonsense"}, "p") == []


def test_absent_subscribes_is_empty_not_error():
    assert manifest_subscribes({"casa": {}}, "p") == []
    assert manifest_subscribes({}, "p") == []
    assert manifest_subscribes({"casa": "nonsense"}, "p") == []


# --- extractor: malformed block raises emits_invalid / subscribes_invalid -


def test_non_list_emits_raises_emits_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_emits(_m_emits({"name": "x"}), "p")
    assert exc.value.reason_code == "emits_invalid"


def test_bad_entry_raises_emits_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_emits(_m_emits(["not-a-dict"]), "p")
    assert exc.value.reason_code == "emits_invalid"


def test_bad_name_charset_raises_emits_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_emits(_m_emits([{"name": "has space"}]), "p")
    assert exc.value.reason_code == "emits_invalid"


def test_non_list_subscribes_raises_subscribes_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_subscribes(_m_subscribes({"plugin": "x", "event": "y"}), "p")
    assert exc.value.reason_code == "subscribes_invalid"


def test_bad_entry_raises_subscribes_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_subscribes(_m_subscribes(["not-a-dict"]), "p")
    assert exc.value.reason_code == "subscribes_invalid"


def test_self_subscribe_raises_subscribes_invalid():
    with pytest.raises(StoreError) as exc:
        manifest_subscribes(
            _m_subscribes([{"plugin": "p", "event": "e"}]), "p")
    assert exc.value.reason_code == "subscribes_invalid"


# --- validate_manifest wiring ----------------------------------------------


def _tree_with_casa(tmp_path, name, casa):
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "casa": casa}),
        encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "s.md").write_text("s", encoding="utf-8")
    return root


def test_validate_manifest_accepts_valid_emits_and_subscribes(tmp_path):
    root = _tree_with_casa(tmp_path, "billing", {
        "emits": [{"name": "invoice-ready"}],
        "subscribes": [{"plugin": "notifier", "event": "ping"}],
    })
    mf = validate_manifest(root, "billing")  # no raise
    assert mf["name"] == "billing"


def test_validate_manifest_refuses_malformed_emits(tmp_path):
    root = _tree_with_casa(tmp_path, "p", {"emits": [
        {"name": "plg-reserved-prefix"}]})
    with pytest.raises(StoreError) as exc:
        validate_manifest(root, "p")
    assert exc.value.reason_code == "emits_invalid"


def test_validate_manifest_refuses_malformed_subscribes(tmp_path):
    root = _tree_with_casa(tmp_path, "p", {"subscribes": [
        {"plugin": "p", "event": "self"}]})
    with pytest.raises(StoreError) as exc:
        validate_manifest(root, "p")
    assert exc.value.reason_code == "subscribes_invalid"


def test_absent_emits_and_subscribes_ok(tmp_path):
    root = _tree_with_casa(tmp_path, "p", {})
    validate_manifest(root, "p")  # no raise


def test_manifest_emits_helper_uses_plugin_name():
    manifest = {"casa": {"emits": [{"name": "ev"}]}}
    emits = manifest_emits(manifest, "el")
    assert emits[0]["effective"] == "plg-el--ev"


# --- artifact_verdict wiring (upgrade-path posture, mirrors callbacks) -----


def _write_artifact(root, *, name, casa):
    import plugin_store

    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "casa": casa}),
        encoding="utf-8")
    (root / "server").mkdir()
    (root / "server" / "server.py").write_text("print('x')\n", encoding="utf-8")
    checksum = plugin_store.content_checksum(root)
    plugin_store.write_metadata(
        root, name=name, repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", artifact_id="a" * 64, version="1.0.0", checksum=checksum)


def test_artifact_verdict_degrades_malformed_emits(tmp_path):
    import plugin_store

    root = tmp_path / "artifact"
    _write_artifact(root, name="p", casa={"emits": [{"name": "plg-reserved"}]})

    verdict = plugin_store.artifact_verdict(
        root, name="p", repo="o/r", revision="git:" + "a" * 40, subdir="",
        artifact_id="a" * 64)
    assert verdict == "emits_invalid"


def test_artifact_verdict_degrades_malformed_subscribes(tmp_path):
    import plugin_store

    root = tmp_path / "artifact"
    _write_artifact(root, name="p", casa={"subscribes": [
        {"plugin": "p", "event": "self"}]})

    verdict = plugin_store.artifact_verdict(
        root, name="p", repo="o/r", revision="git:" + "a" * 40, subdir="",
        artifact_id="a" * 64)
    assert verdict == "subscribes_invalid"


def test_artifact_verdict_accepts_valid_emits_and_subscribes(tmp_path):
    import plugin_store

    root = tmp_path / "artifact"
    _write_artifact(root, name="p", casa={
        "emits": [{"name": "invoice-ready"}],
        "subscribes": [{"plugin": "other", "event": "ping"}],
    })

    verdict = plugin_store.artifact_verdict(
        root, name="p", repo="o/r", revision="git:" + "a" * 40, subdir="",
        artifact_id="a" * 64)
    assert verdict is None
