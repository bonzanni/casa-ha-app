"""Tests for specialist_export.py — N2's production-export tooling (spec §4.5).

Steps 1/7's finance/mtg tests exercise the REAL, currently-bundled image
content (finance's role artifact + persona packs already validated by Plan 1)
so the export tool is proven against the ACTUAL production bytes before
Step 9's no-gap cutover removes them from the image. Step 8 Part C's
clean-image test proves the exported bundle installs once the collision is
gone, using a synthetic clean roles tree — safe to run before the real
cutover happens.
"""
import json
from pathlib import Path

from specialist_export import (
    export_finance_component,
    validate_export_bundle_self_consistency,
    write_export_bundle,
)


def test_export_finance_component_produces_a_self_consistent_bundle() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    defaults_root = repo_root / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
    bundle = export_finance_component(defaults_root=defaults_root)
    assert bundle.slug == "finance"
    assert "manifest.json" in bundle.files
    assert "role/role.yaml" in bundle.files
    assert "role/doctrine.md" in bundle.files
    assert "persona/pack/persona.yaml" in bundle.files
    manifest = json.loads(bundle.files["manifest.json"])
    assert manifest["default_persona"]["ref"].startswith("casa/alex@")


def test_export_finance_component_bundle_writes_and_self_validates(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    defaults_root = repo_root / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
    bundle = export_finance_component(defaults_root=defaults_root)
    write_export_bundle(bundle, tmp_path / "finance-export")
    validate_export_bundle_self_consistency(bundle)  # raises on any inconsistency — no exception here


def test_export_mtg_component_bundles_role_persona_and_corpus(tmp_path: Path) -> None:
    from specialist_export import export_mtg_component, validate_export_bundle_self_consistency

    corpus = tmp_path / "corpus-source"
    corpus.mkdir()
    (corpus / "cr.txt").write_text("702.1 Some rule text.\n", encoding="utf-8")

    repo_root = Path(__file__).resolve().parents[1]
    defaults_root = repo_root / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
    bundle = export_mtg_component(
        defaults_root=defaults_root, corpus_source=corpus,
        mtg_plugin_content_checksum="sha256:" + "5" * 64,
    )
    assert bundle.slug == "mtg"
    assert "corpus/mtg-rules-corpus/cr.txt" in bundle.files
    manifest = json.loads(bundle.files["manifest.json"])
    kinds = {d["kind"] for d in manifest["dependencies"]}
    assert kinds == {"persona", "corpus/data", "plugin/implementation"}
    validate_export_bundle_self_consistency(bundle)


def test_mtg_role_collides_with_finance_pattern_while_still_bundled_in_image() -> None:
    """Documents the transient collision this task's Files section flags —
    proves _discover_image_role_slots sees the staging-only mtg role
    artifact exactly like it sees finance, until Step 9 removes both.

    TRANSIENT: this test is deleted again in Step 9's cutover commit — once
    finance/mtg are actually removed from the image, this assertion becomes
    false by design (see test_finance_and_mtg_are_no_longer_bundled_in_the_image
    in tests/test_resident_domain_boundary.py, its mirror-image regression)."""
    from specialist_registry import _discover_image_role_slots

    slots = _discover_image_role_slots()
    assert "mtg" in slots  # staging-only role artifact, present until Step 9
    assert "finance" in slots
