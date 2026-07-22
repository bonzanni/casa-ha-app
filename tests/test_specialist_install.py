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
    commit_specialist_install,
    parse_component_root,
    resolve_dependency_closure,
)
from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
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


# ---------------------------------------------------------------------------
# commit_specialist_install (Step 12)
# ---------------------------------------------------------------------------


def _staged_inspection(tmp_path: Path) -> "specialist_install.InspectionResult":
    from specialist_install import InspectionResult, compute_install_root_digest

    root = _write_component(tmp_path / "component", slug="mtg")
    component = load_specialist_component(root, root / "manifest.json")
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(root / "manifest.json").read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission=str(component.role.role["mission"]),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=root,
    )


def test_commit_refuses_without_a_recorded_consent_ack(tmp_path: Path) -> None:
    inspection = _staged_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")  # never recorded
    with pytest.raises(SpecialistInstallError) as raised:
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents-specialists",
        )
    assert raised.value.kind == "consent_missing"
    assert not (tmp_path / "specialists" / "mtg").exists()  # nothing persisted


def test_commit_persists_cas_writes_active_tuple_and_materializes_operational_files(
    tmp_path: Path,
) -> None:
    inspection = _staged_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "active"
    assert instance.active is not None
    assert instance.active.binding.mode == "component-default"
    assert instance.last_activation_error is None  # happy path: no self-heal note needed
    component_id, version, checksum = parse_component_root(instance.active.root)
    assert checksum == inspection.root_digest

    cas_role = tmp_path / "specialists" / "store" / checksum.removeprefix("sha256:") / "role"
    assert (cas_role / "role.yaml").is_file()
    op_dir = tmp_path / "agents-specialists" / "mtg"
    for name in ("character.yaml", "voice.yaml", "response_shape.yaml", "runtime.yaml"):
        assert (op_dir / name).is_file(), name


def test_commit_survives_a_materialize_failure_and_self_heals_on_next_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 fix (finding #2): commit_desired_to_active runs BEFORE
    materialize, so a materialize failure must NOT roll back the already-
    committed tuple — and the NEXT current_specialist_roles_dir call must
    repair the operational files with no operator action.

    N1b slice C converges this test onto the REAL `InstalledSpecialistIndex.
    installed_component_role_dirs()` (Step 17, specialist_registry.py) — the
    test-local `_IndexWithRoleDirs` forward shim slice B carried is gone;
    this drives `current_specialist_roles_dir` end-to-end against the real
    index, no subclass needed.
    """
    import specialist_materialize

    inspection = _staged_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    original_materialize = specialist_materialize.materialize_specialist_operational_files
    call_count = {"n": 0}

    def _flaky_materialize(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated disk-full on first materialize")
        return original_materialize(**kwargs)

    monkeypatch.setattr(specialist_materialize, "materialize_specialist_operational_files",
                         _flaky_materialize)
    # commit_specialist_install does a LOCAL `import specialist_materialize` — same
    # module object from sys.modules, so patching the attribute above is sufficient;
    # no separate patch of specialist_install's own namespace is needed or correct
    # (it has no module-level `specialist_materialize` name to patch).

    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    # The tuple is committed and active DESPITE the materialize failure —
    # never rolled back for a derived-cache write failure.
    assert instance.state == "active"
    assert instance.active is not None
    assert instance.last_activation_error is not None
    assert "pending reconcile" in instance.last_activation_error
    assert not (agents_specialists_dir / "mtg").exists()  # materialize genuinely never ran

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    roles_dir = specialist_materialize.current_specialist_roles_dir(
        installed_index=index, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    assert roles_dir  # roles overlay still reconciled even though op-files needed a retry
    op_dir = agents_specialists_dir / "mtg"
    for name in ("character.yaml", "voice.yaml", "response_shape.yaml", "runtime.yaml"):
        assert (op_dir / name).is_file(), name  # self-healed with no operator action


def test_commit_with_missing_required_config_yields_pending_configuration(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component", slug="mtg")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # This test's component declares no required config in its schema — rebuild
    # with one required, non-secret key to exercise the pending path.
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["timezone"], "secret_names": []}), encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from specialist_install import InspectionResult, compute_install_root_digest

    component = load_specialist_component(root, manifest_path)
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("timezone",), required_secret_names=(), dependencies=deps,
        staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "pending-configuration"
    assert instance.active is None
    assert instance.desired is not None
    assert not (tmp_path / "agents-specialists" / "mtg").exists()  # not materialized while pending


# ---------------------------------------------------------------------------
# upgrade_specialist / rollback_specialist / uninstall_specialist (Task N1c)
# ---------------------------------------------------------------------------


def _installed_mtg(tmp_path: Path) -> tuple[Path, Path, "specialist_install.InspectionResult"]:
    """Shared setup: a committed, active mtg install at version 0.1.0."""
    from specialist_component import load_specialist_component
    from specialist_install import InspectionResult, commit_specialist_install, resolve_dependency_closure
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    staged = _write_component(tmp_path / "staged-v1", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    from specialist_install import compute_install_root_digest
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)
    specialists_dir, agents_specialists_dir = tmp_path / "specialists", tmp_path / "agents-specialists"
    commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    return specialists_dir, agents_specialists_dir, inspection


def _v2_inspection(tmp_path: Path) -> "specialist_install.InspectionResult":
    from specialist_component import load_specialist_component
    from specialist_install import InspectionResult, resolve_dependency_closure

    staged = _write_component(tmp_path / "staged-v2", slug="mtg")
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "0.2.0"
    files = {
        "role/role.yaml": (staged / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (staged / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (staged / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    component = load_specialist_component(staged, manifest_path)
    deps = resolve_dependency_closure(component, staged)
    from specialist_install import compute_install_root_digest
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
    )


def test_upgrade_commits_a_new_active_tuple_and_retains_the_prior_as_rollback_target(
    tmp_path: Path,
) -> None:
    from specialist_install import upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         component_checksum=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)

    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    assert instance.state == "active"
    assert instance.active.binding.persona_checksum  # sanity: compiled successfully
    assert (specialists_dir / "mtg" / "active.prior.yaml").exists()


def test_upgrade_with_missing_new_required_config_leaves_the_active_tuple_running(
    tmp_path: Path,
) -> None:
    from specialist_install import upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    manifest_path = v2.staged_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (v2.staged_dir / "config-schema.json").write_text(
        json.dumps({"required": ["new_secret_flag"], "secret_names": ["new_secret_flag"]}),
        encoding="utf-8")
    files = {
        "role/role.yaml": (v2.staged_dir / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (v2.staged_dir / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (v2.staged_dir / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    from specialist_install import (
        InspectionResult, compute_install_root_digest, resolve_dependency_closure,
    )
    component = load_specialist_component(v2.staged_dir, manifest_path)
    v2_deps = resolve_dependency_closure(component, v2.staged_dir)
    v2_root_digest = compute_install_root_digest(
        component, v2_deps, manifest_bytes=manifest_path.read_bytes())
    v2 = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=v2_root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=("new_secret_flag",),
        dependencies=v2_deps, staged_dir=v2.staged_dir,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         component_checksum=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)

    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    assert instance.state == "pending-configuration"
    assert instance.active is not None  # the OLD (v1) active tuple keeps running
    assert instance.active.root != instance.desired.root  # desired is the staged v2 candidate


def test_rollback_restores_the_prior_tuple(tmp_path: Path) -> None:
    from specialist_install import parse_component_root, rollback_specialist, upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         component_checksum=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)
    upgrade_specialist(slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(),
                        acks=acks, specialists_dir=specialists_dir,
                        agents_specialists_dir=agents_specialists_dir)

    rolled_back = rollback_specialist(
        slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir)
    assert rolled_back.active.binding.component_root is not None
    _, _, checksum = parse_component_root(rolled_back.active.root)
    assert checksum == v1.root_digest  # back to the pre-upgrade version


def test_rollback_with_no_prior_tuple_raises(tmp_path: Path) -> None:
    from specialist_install import rollback_specialist

    specialists_dir, agents_specialists_dir, _v1 = _installed_mtg(tmp_path)
    with pytest.raises(SpecialistInstallError) as raised:
        rollback_specialist(slug="mtg", specialists_dir=specialists_dir,
                             agents_specialists_dir=agents_specialists_dir)
    assert raised.value.kind == "no_prior_tuple"


def test_uninstall_removes_the_instance_dir_and_operational_files(tmp_path: Path) -> None:
    from specialist_install import uninstall_specialist

    specialists_dir, agents_specialists_dir, _v1 = _installed_mtg(tmp_path)
    op_dir = agents_specialists_dir / "mtg"
    assert op_dir.is_symlink()  # sanity: the fixture installed via the real pipeline
    content_dir = agents_specialists_dir / os.readlink(op_dir)

    uninstall_specialist(slug="mtg", specialists_dir=specialists_dir,
                          agents_specialists_dir=agents_specialists_dir)
    assert not (specialists_dir / "mtg").exists()
    assert not os.path.lexists(op_dir)  # Round-4 fix (finding #1): symlink itself is gone, not
                                          # just dangling (shutil.rmtree silently no-ops on a symlink)
    assert not content_dir.exists()  # and its versioned content directory with it


# ---------------------------------------------------------------------------
# cas_pin_roots / persona_pin_roots (Task N1d, spec §4.4)
# ---------------------------------------------------------------------------


def test_cas_pin_roots_includes_active_desired_and_prior_checksums(tmp_path: Path) -> None:
    from specialist_install import cas_pin_roots, upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         component_checksum=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)
    upgrade_specialist(slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(),
                        acks=acks, specialists_dir=specialists_dir,
                        agents_specialists_dir=agents_specialists_dir)

    # cas_pin_roots parses component_root, which embeds the full-closure
    # root_digest (Round-2, finding #2) — not the narrow component_checksum.
    pinned = cas_pin_roots(specialists_dir)
    assert v1.root_digest in pinned  # retained via active.prior.yaml
    assert v2.root_digest in pinned  # current active


def test_cas_pin_roots_on_missing_directory_returns_empty(tmp_path: Path) -> None:
    from specialist_install import cas_pin_roots

    assert cas_pin_roots(tmp_path / "does-not-exist") == frozenset()


def test_cas_pin_roots_pins_an_override_bound_specialists_component_root(tmp_path: Path) -> None:
    """Round-2 fix (finding #8/#4's exposed bug): an OVERRIDE-mode specialist
    binding has `binding.component_root is None` (only component-default
    populates it) — cas_pin_roots must still pin the component blob via
    `InstanceTuple.root` (which apply_persona_override never rewrites for a
    specialist target), not via `binding.component_root`."""
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from specialist_install import cas_pin_roots, cas_store_dir, parse_component_root
    from personality_binding import InstanceDir
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    specialists_dir, _agents_dir, v1 = _installed_mtg(tmp_path)
    active = InstanceDir(specialists_dir / "mtg").active()
    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})

    # persona_id/version must satisfy the mtg role's persona_requirements
    # ("casa/judge@>=0.1.0 <1.0.0", from _write_component above) — a
    # DIFFERENT version of the SAME compatible slug, not an unrelated one.
    override_dir = tmp_path / "judge2-repo"
    persona_yaml = {
        "api_version": "casa.persona/v1", "id": "casa/judge", "version": "0.2.0",
        "trait_schema_version": 1,
        "identity": {"display_name": "Judge Two", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        "relationship_posture": "established", "archetype": "adjudicator",
        "traits": {"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                    "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        "quirks": [],
    }
    (override_dir / "pack").mkdir(parents=True)
    (override_dir / "pack" / "persona.yaml").write_text(
        yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    core = "Q" * 350
    (override_dir / "pack" / "persona.md").write_text(
        f"# Core\n\n{core}\n\n## Negative space\n\nNever guesses.\n", encoding="utf-8")
    rows = []
    for name in sorted(os.listdir(override_dir / "pack")):
        text = canonical_text((override_dir / "pack" / name).read_text(encoding="utf-8"))
        rows.append({"path": name, "type": "file", "executable": False,
                      "checksum": checksum_bytes(text.encode("utf-8"))})
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    payload["checksum"] = checksum_bytes(canonical_json_bytes(payload))
    (override_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    persona = load_persona_pack(override_dir / "pack", override_dir / "manifest.json")

    apply_persona_override(
        target_role_id="specialist:mtg", persona=persona, role=role,
        instance_dir_root=specialists_dir / "mtg",
    )
    pinned = cas_pin_roots(specialists_dir)
    assert v1.root_digest in pinned  # the component blob is STILL pinned post-override


def test_persona_pin_roots_always_includes_image_defaults(tmp_path: Path) -> None:
    from specialist_install import persona_pin_roots
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT

    pinned = persona_pin_roots(
        bindings_dir=tmp_path / "bindings", specialists_dir=tmp_path / "specialists")
    for ref in IMAGE_DEFAULT_PERSONA_BY_SLOT.values():
        assert ref in pinned


def test_persona_pin_roots_includes_a_resident_override_binding(tmp_path: Path) -> None:
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from role_artifact import load_role_artifact
    from role_slot import materialize_role
    from specialist_install import persona_pin_roots
    from test_persona_install import _write_persona_repo

    role_dir = (
        Path(__file__).resolve().parent.parent
        / "casa-agent/rootfs/opt/casa/defaults/roles/resident/assistant"
    )
    role = materialize_role(source=load_role_artifact(role_dir), options={})
    persona_dir = tmp_path / "ellen-repo"
    _write_persona_repo(persona_dir, persona_id="casa/ellen", version="0.1.0")
    persona = load_persona_pack(persona_dir / "pack", persona_dir / "manifest.json")

    bindings_dir = tmp_path / "bindings"
    apply_persona_override(
        target_role_id="resident:assistant", persona=persona, role=role,
        instance_dir_root=bindings_dir / "resident-assistant",
    )

    pinned = persona_pin_roots(bindings_dir=bindings_dir, specialists_dir=tmp_path / "specialists")
    assert "casa/ellen@0.1.0" in pinned


def test_persona_pin_roots_includes_an_override_bound_specialist(tmp_path: Path) -> None:
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from personality_binding import InstanceDir
    from role_artifact import load_role_artifact
    from role_slot import materialize_role
    from specialist_install import cas_store_dir, parse_component_root, persona_pin_roots
    from test_persona_install import _write_persona_repo

    specialists_dir, _agents_dir, _v1 = _installed_mtg(tmp_path)
    active = InstanceDir(specialists_dir / "mtg").active()
    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})

    # persona_id/version must satisfy the mtg role's persona_requirements
    # ("casa/judge@>=0.1.0 <1.0.0", from _write_component above).
    persona_dir = tmp_path / "judge3-repo"
    _write_persona_repo(persona_dir, persona_id="casa/judge", version="0.3.0")
    persona = load_persona_pack(persona_dir / "pack", persona_dir / "manifest.json")

    apply_persona_override(
        target_role_id="specialist:mtg", persona=persona, role=role,
        instance_dir_root=specialists_dir / "mtg",
    )

    pinned = persona_pin_roots(bindings_dir=tmp_path / "bindings", specialists_dir=specialists_dir)
    assert "casa/judge@0.3.0" in pinned
