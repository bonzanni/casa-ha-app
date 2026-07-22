import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

import specialist_install
from specialist_install import (
    DependencyResolution,
    SpecialistInstallError,
    resolve_dependency_closure,
)
from specialist_component import compute_component_checksum, load_specialist_component
from personality_binding import BindingRecord, InstanceDir, InstanceTuple, compute_binding_digest
from specialist_registry import InstalledSpecialistIndex
from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes


def _write_component(root: Path, *, slug: str = "mtg-test",
                      dependencies: list[dict] | None = None) -> Path:
    (root / "role").mkdir(parents=True)
    (root / "persona" / "pack").mkdir(parents=True)
    role_yaml = {
        "api_version": "casa.role/v1", "id": f"specialist:{slug}", "kind": "specialist",
        "slot": slug, "mission": "Answer test questions.", "enabled": True,
        "model": {"source": "fixed", "value": "sonnet"},
        "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                   "max_turns": 8, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [], "channels": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "delegated", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                      "restricted_webhook": {"register": "plain"}},
        "persona": {"policy": "required", "compatibility": ["casa/judge@>=0.1.0 <1.0.0"]},
        "requires": {"plugins": [], "tools": []}, "doctrine_file": "doctrine.md",
    }
    (root / "role" / "role.yaml").write_text(yaml.safe_dump(role_yaml, sort_keys=False), encoding="utf-8")
    (root / "role" / "doctrine.md").write_text("# Core doctrine\n\nAnswer test questions.\n", encoding="utf-8")
    config_schema = {"required": [], "secret_names": []}
    (root / "config-schema.json").write_text(json.dumps(config_schema), encoding="utf-8")

    persona_yaml = {
        "api_version": "casa.persona/v1", "id": "casa/judge", "version": "0.1.0",
        "trait_schema_version": 1,
        "identity": {"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        "relationship_posture": "established", "archetype": "adjudicator",
        "traits": {"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                    "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        "quirks": [],
    }
    (root / "persona" / "pack" / "persona.yaml").write_text(
        yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    core = "X" * 350
    (root / "persona" / "pack" / "persona.md").write_text(
        f"# Core\n\n{core}\n\n## Negative space\n\nNever guesses.\n", encoding="utf-8")
    manifest_rows = []
    # persona_pack._admit_files sorts admitted files by NAME
    # ("persona.md" < "persona.yaml" alphabetically) — the manifest row
    # order must match that sort, not source-declaration order, or
    # load_persona_pack's own recomputed manifest payload (and hence its
    # checksum) will never equal what's written to disk here.
    for name in sorted(os.listdir(root / "persona" / "pack")):
        text = canonical_text((root / "persona" / "pack" / name).read_text(encoding="utf-8"))
        manifest_rows.append({"path": name, "type": "file", "executable": False,
                               "checksum": checksum_bytes(text.encode("utf-8"))})
    persona_manifest_payload = {"api_version": "casa.persona.manifest/v1", "files": manifest_rows}
    persona_checksum = checksum_bytes(canonical_json_bytes(persona_manifest_payload))
    persona_manifest_payload["checksum"] = persona_checksum
    (root / "persona" / "manifest.json").write_text(json.dumps(persona_manifest_payload), encoding="utf-8")

    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    from specialist_component import compute_component_checksum
    component_checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1", "component_id": f"casa-test/{slug}",
        "version": "0.1.0",
        "default_persona": {"ref": "casa/judge@0.1.0", "checksum": persona_checksum},
        # Controller resolution B: a dependency row is exactly
        # {kind, identifier, digest} — specialist-component.v1.json sets
        # additionalProperties: false, so any extra key (the brief's stale
        # "checksum_field_unused") would fail schema validation.
        "dependencies": dependencies if dependencies is not None else [
            {"kind": "persona", "identifier": "casa/judge@0.1.0", "digest": persona_checksum},
        ],
        "checksum": component_checksum,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_resolve_dependency_closure_marks_bundled_persona_available(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component")
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    persona_rows = [r for r in resolutions if r.kind == "persona"]
    assert len(persona_rows) == 1
    assert persona_rows[0].available is True


def test_resolve_dependency_closure_reports_missing_corpus(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component", dependencies=[
        {"kind": "corpus/data", "identifier": "mtg-rules-corpus", "digest": "sha256:" + "9" * 64},
    ])
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    assert resolutions[0].available is False
    assert "corpus" in resolutions[0].detail


def test_resolve_dependency_closure_matches_corpus_digest(tmp_path: Path) -> None:
    from plugin_store import content_checksum

    corpus_dir = tmp_path / "component" / "corpus" / "mtg-rules-corpus"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "cr.txt").write_text("702.1 Some rule text.\n", encoding="utf-8")
    # plugin_store.content_checksum returns a BARE hex digest; the
    # specialist-component.v1.json schema constrains a dependency row's
    # `digest` field to `sha256:<hex>` — the brief's test used the bare
    # digest directly, which would fail schema validation at
    # load_specialist_component. Prefix it here, matching the normalization
    # resolve_dependency_closure itself applies to the same bare value.
    digest = "sha256:" + content_checksum(corpus_dir)
    root = _write_component(tmp_path / "component", dependencies=[
        {"kind": "corpus/data", "identifier": "mtg-rules-corpus", "digest": digest},
    ])
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    assert resolutions[0].available is True


# ---------------------------------------------------------------------------
# inspect_specialist_repo — direct regression coverage (fix round 1).
#
# resolve_and_fetch does real network/git I/O (plugin_store.resolve_ref +
# fetch_commit_tree). Every test here monkeypatches the MODULE ATTRIBUTE
# specialist_install.resolve_and_fetch with a stub that simply copies an
# already-built local component tree (via _write_component, above) into the
# caller-supplied `dest` and returns a fake 40-hex commit sha — never
# patching asyncio.sleep or any stdlib global (see CLAUDE.md's memory-cage
# note on why that specific pattern is forbidden in this repo).
# ---------------------------------------------------------------------------


def _stub_resolve_and_fetch(component_root: Path):
    """Build a stub matching resolve_and_fetch's exact signature
    (repo, ref, subdir, dest, *, expected_revision=None) -> str, so a
    monkeypatched call site sees identical call ergonomics to the real
    thing — it just copies a pre-built tree instead of fetching one."""

    def _stub(repo: str, ref: str, subdir: str, dest: Path, *, expected_revision: str | None = None) -> str:
        shutil.copytree(component_root, dest)
        return "a" * 40

    return _stub


def _specialist_binding(slug: str, **overrides) -> BindingRecord:
    """Mirrors tests/test_personality_binding.py's own `_binding` helper:
    build a schema-valid BindingRecord by actually recomputing
    binding_digest from the other fields via compute_binding_digest,
    rather than hand-picking an arbitrary digest string — a mismatched
    digest would fail verify_binding_record's own integrity check the
    moment InstanceDir.active()/desired() re-reads it off disk."""
    fields = dict(
        stable_agent_id=f"specialist:{slug}",
        role_checksum="sha256:" + "1" * 64,
        persona_id="casa/judge",
        persona_version="0.1.0",
        persona_checksum="sha256:" + "2" * 64,
        compiler_schema_version="v1",
        dependency_digests=(),
        effective_config_digest="sha256:" + "3" * 64,
    )
    fields.update({k: v for k, v in overrides.items() if k in fields})
    digest = compute_binding_digest(**fields)
    return BindingRecord(
        **fields, mode="component-default", binding_digest=digest,
        component_root=overrides.get("component_root", f"casa-test/{slug}@0.1.0"),
    )


def _specialist_tuple(binding: BindingRecord) -> InstanceTuple:
    return InstanceTuple(
        root=binding.component_root or "", binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    )


def _write_component_with_role_yaml_comment_marker(root: Path, *, slug: str) -> Path:
    """A forbidden marker hidden in a YAML COMMENT. role_artifact.
    load_role_artifact's own marker check (authored_markers.
    reject_markers_in_parsed) walks the PARSED tree's string leaves —
    YAML comments are stripped by the parser and never appear there, so
    that check does not see this marker and load_specialist_component
    succeeds. specialist_install._validate_untrusted_bytes's raw-TEXT
    scan of role.yaml (component.role.role_path.read_text(...)) is the
    only thing that catches it — empirically confirmed (see fix-round-1
    report) that a doctrine.md-body marker is instead caught earlier, by
    load_role_artifact itself, and surfaces as kind='manifest_invalid',
    not 'forbidden_markers' — so this fixture exercises the genuine
    belt-and-suspenders gap _validate_untrusted_bytes exists to close."""
    root = _write_component(root, slug=slug)
    role_yaml_path = root / "role" / "role.yaml"
    original = role_yaml_path.read_text(encoding="utf-8")
    tampered = f"# a marker hidden in a comment: ${{SECRET}}\n{original}"
    role_yaml_path.write_text(tampered, encoding="utf-8")

    files = {
        "role/role.yaml": role_yaml_path.read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_inspect_specialist_repo_install_mode_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_root = _write_component(tmp_path / "component", slug="fresh-specialist")
    component = load_specialist_component(component_root, component_root / "manifest.json")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "main",
        staging_root=tmp_path / "staging",
        installed_index=index,
    )
    assert result.slug == "fresh-specialist"
    assert result.component_checksum == component.checksum
    assert result.root_digest != result.component_checksum


def test_inspect_specialist_repo_install_mode_rejects_already_installed_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "collide-me"
    component_root = _write_component(tmp_path / "component", slug=slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    specialists_dir = tmp_path / "specialists"
    InstanceDir(specialists_dir / slug).stage_desired(_specialist_tuple(_specialist_binding(slug)))
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    assert slug in index.installed_slugs()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "slug_collision"


def test_inspect_specialist_repo_upgrade_mode_requires_target_slug(tmp_path: Path) -> None:
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", mode="upgrade",
            staging_root=tmp_path / "staging",
        )
    assert exc_info.value.kind == "target_slug_required"


def test_inspect_specialist_repo_upgrade_mode_rejects_slug_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_root = _write_component(tmp_path / "component", slug="actual-slug")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", mode="upgrade", target_slug="other-slug",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "slug_mismatch"


def test_inspect_specialist_repo_upgrade_mode_requires_active_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "needs-active"
    component_root = _write_component(tmp_path / "component", slug=slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    specialists_dir = tmp_path / "specialists"
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", mode="upgrade", target_slug=slug,
            staging_root=tmp_path / "staging",
            installed_index=index,
            specialists_dir=specialists_dir,
        )
    assert exc_info.value.kind == "no_active_tuple"


def test_inspect_specialist_repo_upgrade_mode_succeeds_and_excludes_only_target_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_slug = "upgrade-target"
    other_slug = "still-collides"
    specialists_dir = tmp_path / "specialists"

    # target_slug has an ACTIVE tuple committed -> upgrade is sanctioned.
    target_dir = InstanceDir(specialists_dir / target_slug)
    target_dir.stage_desired(_specialist_tuple(_specialist_binding(target_slug)))
    target_dir.commit_desired_to_active()

    # other_slug is ALSO installed (pending-configuration is enough to
    # count towards installed_slugs()) but is NOT the upgrade target.
    InstanceDir(specialists_dir / other_slug).stage_desired(
        _specialist_tuple(_specialist_binding(other_slug)))

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    assert index.installed_slugs() == {target_slug, other_slug}

    component_root = _write_component(tmp_path / "component", slug=target_slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "main", mode="upgrade", target_slug=target_slug,
        staging_root=tmp_path / "staging",
        installed_index=index,
        specialists_dir=specialists_dir,
    )
    assert result.slug == target_slug

    # Same index, same OTHER (non-excluded) slug, a plain fresh install
    # attempt must still collide — upgrade mode narrowly excludes only
    # target_slug, never any other already-installed slug.
    other_component_root = _write_component(tmp_path / "component-other", slug=other_slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(other_component_root))
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo2", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "slug_collision"


def test_inspect_specialist_repo_rejects_dependency_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_root = _write_component(tmp_path / "component", slug="needs-corpus", dependencies=[
        {"kind": "corpus/data", "identifier": "missing-corpus", "digest": "sha256:" + "9" * 64},
    ])
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "dependency_unavailable"


def test_inspect_specialist_repo_rejects_forbidden_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_root = _write_component_with_role_yaml_comment_marker(
        tmp_path / "component", slug="marker-test")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "forbidden_markers"
