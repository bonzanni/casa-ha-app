"""Pinning tests for plugin-subsystem invariants (docs corpus).

Each test names the corpus invariant it pins and records, in its docstring, the
red case that was demonstrated: the code edit that made it fail. A pinning test
never shown red proves nothing.
"""
import json

from plugin_store import artifact_verdict, content_checksum, write_metadata


def _make_artifact(root):
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    (root / "skill.md").write_text("clean", encoding="utf-8")
    write_metadata(root, name="p", repo="o/r", ref="v1",
                   revision="git:" + "a" * 40, subdir="",
                   artifact_id="a" * 64, version="1.0.0",
                   checksum=content_checksum(root))


def _verdict(path):
    return artifact_verdict(path, name="p", repo="o/r",
                            revision="git:" + "a" * 40, subdir="",
                            artifact_id="a" * 64)


def test_pin_inv_plug_002_checksum_drift_and_symlinked_locations(tmp_path):
    """INV-PLUG-002: a resolved artifact must match its recorded content
    checksum, and neither the artifact path nor its parent may be a symlink.

    Red cases demonstrated: deleting artifact_verdict's checksum-mismatch
    branch makes the tampered artifact return None (first assertion fails);
    weakening the symlink refusal from `or` to `and` makes both symlinked
    locations usable (second and third assertions fail).
    """
    artifact = tmp_path / "artifact"
    _make_artifact(artifact)
    (artifact / "skill.md").write_text("tampered", encoding="utf-8")
    assert _verdict(artifact) == "corrupt_artifact"

    linked_artifact = tmp_path / "linked-artifact"
    linked_artifact.symlink_to(artifact)
    assert _verdict(linked_artifact) == "artifact_invalid"

    real_parent = tmp_path / "real-parent"
    real_artifact = real_parent / ("a" * 64)
    _make_artifact(real_artifact)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent)
    assert _verdict(linked_parent / ("a" * 64)) == "artifact_invalid"
