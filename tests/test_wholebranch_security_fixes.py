"""Whole-branch adversarial-review security regressions (fix wave 1).

One test per finding, each proving a fail-closed invariant that the pre-fix
code violated (RED-first). Findings:

* F1 — slug / persona-ref / corpus-identifier traversal in lifecycle functions
* F2 — symlink-target containment on uninstall + re-materialize GC
* F3 — persona identity binding (checksum match is not identity proof)
* F4 — corpus identifier containment (schema-unconstrained -> path join)
* F5 — fail-closed self-heal on op-file / tuple divergence
* F6 — rollback verification gate (dependency closure + compile)
* Md — concurrent-install CAS race

Shares the component fixture helpers from test_specialist_install /
test_specialist_lifecycle_matrix so a component here is byte-identical to the
ones the rest of the suite installs."""
import json
import os
import shutil
from pathlib import Path

import pytest

import specialist_install
from specialist_install import (
    SpecialistInstallError,
    commit_specialist_install,
    is_safe_corpus_identifier,
    parse_component_root,
    resolve_dependency_closure,
    rollback_specialist,
    uninstall_specialist,
    upgrade_specialist,
    validate_specialist_slug,
)
from specialist_component import load_specialist_component
from test_specialist_install import _write_component
from test_specialist_lifecycle_matrix import _approved_inspection


# --------------------------------------------------------------------------
# F1 — traversal in lifecycle functions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../../etc", "/data", "a/b", "..", "", "MTG", "a b"])
def test_validate_specialist_slug_rejects_unsafe_slugs(bad: str) -> None:
    with pytest.raises(SpecialistInstallError) as exc:
        validate_specialist_slug(bad)
    assert exc.value.kind == "invalid_slug"


def test_uninstall_specialist_rejects_traversal_slug_leaving_siblings_intact(
    tmp_path: Path,
) -> None:
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    specialists_dir.mkdir()
    agents_dir.mkdir()
    # A canary directory a naive `specialists_dir / slug` rmtree would delete.
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "keep.txt").write_text("precious", encoding="utf-8")

    with pytest.raises(SpecialistInstallError) as exc:
        uninstall_specialist(
            slug="../canary", specialists_dir=specialists_dir,
            agents_specialists_dir=agents_dir)
    assert exc.value.kind == "invalid_slug"
    # RED on pre-fix: `specialists_dir / "../canary"` == the canary dir, which
    # the old shutil.rmtree would have deleted.
    assert (canary / "keep.txt").is_file()


def test_rollback_specialist_rejects_traversal_slug(tmp_path: Path) -> None:
    with pytest.raises(SpecialistInstallError) as exc:
        rollback_specialist(
            slug="../evil", specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents")
    assert exc.value.kind == "invalid_slug"


def test_upgrade_specialist_rejects_traversal_slug(tmp_path: Path) -> None:
    inspection, acks = _approved_inspection(tmp_path, slug="mtg")
    with pytest.raises(SpecialistInstallError) as exc:
        upgrade_specialist(
            slug="../evil", inspection=inspection, config={},
            secret_names_provided=frozenset(), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents")
    assert exc.value.kind == "invalid_slug"


def test_inspect_specialist_repo_rejects_traversal_target_slug(tmp_path: Path) -> None:
    from specialist_install import inspect_specialist_repo

    with pytest.raises(SpecialistInstallError) as exc:
        inspect_specialist_repo(
            "casa-test/x", "main", mode="upgrade", target_slug="../evil",
            staging_root=tmp_path / "staging", specialists_dir=tmp_path / "specialists")
    assert exc.value.kind == "invalid_slug"


def test_commit_persona_install_rejects_traversal_persona_id(tmp_path: Path) -> None:
    from persona_install import (
        PersonaInspectionResult, PersonaInstallAckStore, commit_persona_install,
        persona_install_consent_identity,
    )

    checksum = "sha256:" + "1" * 64
    insp = PersonaInspectionResult(
        persona_id="../evil", version="0.1.0", checksum=checksum,
        display_name="x", staged_dir=tmp_path / "staged")
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    ident = persona_install_consent_identity(
        persona_id="../evil", version="0.1.0", checksum=checksum)
    acks.record(identity=ident, persona_id="../evil", version="0.1.0", checksum=checksum)

    with pytest.raises(SpecialistInstallError) as exc:
        commit_persona_install(
            inspection=insp, acks=acks, personas_root=tmp_path / "personas")
    assert exc.value.kind == "invalid_persona_ref"


def test_validate_persona_path_segments_rejects_unsafe(tmp_path: Path) -> None:
    from persona_install import validate_persona_path_segments

    for pid in ("../evil", "/abs/x", "a/b/c", "casa/x/y", ""):
        with pytest.raises(SpecialistInstallError) as exc:
            validate_persona_path_segments(pid, "0.1.0")
        assert exc.value.kind == "invalid_persona_ref"
    for ver in ("../1", "0.1", "latest", ""):
        with pytest.raises(SpecialistInstallError) as exc:
            validate_persona_path_segments("casa/x", ver)
        assert exc.value.kind == "invalid_persona_ref"
    # A well-formed namespaced ref + semver passes.
    validate_persona_path_segments("casa/alex", "0.1.0")


# --------------------------------------------------------------------------
# F2 — symlink-target containment
# --------------------------------------------------------------------------


def test_uninstall_specialist_symlink_to_absolute_target_is_never_rmtreed(
    tmp_path: Path,
) -> None:
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    specialists_dir.mkdir()
    agents_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("precious", encoding="utf-8")

    # A pre-existing malicious/accidental symlink whose target is an absolute
    # external directory (finance -> /external).
    os.symlink(str(external), agents_dir / "finance")

    uninstall_specialist(
        slug="finance", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # RED on pre-fix: `agents_dir / os.readlink(...)` == the absolute external
    # dir, which the old rmtree would have deleted.
    assert (external / "keep.txt").is_file()
    # The symlink itself is removed (uninstall is not a silent no-op).
    assert not (agents_dir / "finance").exists()
    assert not (agents_dir / "finance").is_symlink()


def test_materialize_prior_symlink_to_absolute_target_is_never_gcd(tmp_path: Path) -> None:
    from specialist_materialize import materialize_specialist_operational_files
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    role = RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg",
        mission="Answer questions.",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={
            "model": {"source": "fixed", "value": "sonnet"},
            "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                       "max_turns": 8, "skills": "none", "voice_guard": "none"},
            "mcp_servers": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
            "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
            "tts": {"tag_dialect": "none", "error_phrases": {}},
            "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                          "restricted_webhook": {"register": "plain"}},
            "requires": {"plugins": [], "tools": []},
        },
        doctrine="# Core doctrine\n\nAnswer.\n", checksum="sha256:" + "1" * 64,
    )
    persona = PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="adjudicator",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges.\n\n## Negative space\n\nNever guesses.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("precious", encoding="utf-8")
    # A pre-existing hostile slug symlink whose target escapes the directory.
    os.symlink(str(external), agents_dir / "mtg")

    materialize_specialist_operational_files(
        agents_specialists_dir=agents_dir, slug="mtg", role=role, persona=persona)

    # The symlink was retargeted to the new content dir (materialize still
    # works), but the escaping prior target was NOT garbage-collected.
    assert (external / "keep.txt").is_file()
    assert (agents_dir / "mtg" / "runtime.yaml").is_file()  # new content live


# --------------------------------------------------------------------------
# F3 — persona identity binding
# --------------------------------------------------------------------------


def test_persona_dependency_identity_mismatch_is_unavailable(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # The bundled pack is casa/judge@0.1.0 with the RIGHT digest, but the
    # dependency line NAMES a different persona — a substitution.
    good_digest = manifest["dependencies"][0]["digest"]
    manifest["dependencies"] = [
        {"kind": "persona", "identifier": "casa/impostor@0.1.0", "digest": good_digest}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    component = load_specialist_component(root, manifest_path)
    rows = [r for r in resolve_dependency_closure(component, root) if r.kind == "persona"]
    assert len(rows) == 1
    # RED on pre-fix: the checksum matched, so it was marked available=True.
    assert rows[0].available is False
    assert "not the declared dependency" in rows[0].detail


def test_persona_dependency_disagreeing_with_default_persona_is_unavailable(
    tmp_path: Path,
) -> None:
    root = _write_component(tmp_path / "component")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # Dependency identifier matches the bundled pack, but the component's
    # declared default_persona ref points elsewhere — an internal inconsistency
    # the operator's consent (which shows default_persona) must not paper over.
    manifest["default_persona"]["ref"] = "casa/other@0.1.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    component = load_specialist_component(root, manifest_path)
    rows = [r for r in resolve_dependency_closure(component, root) if r.kind == "persona"]
    assert rows[0].available is False
    assert "default_persona" in rows[0].detail


# --------------------------------------------------------------------------
# F4 — corpus identifier containment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../canary", "/etc", "a/b", "..", "", "A", "a/../b"])
def test_is_safe_corpus_identifier_rejects_unsafe(bad: str) -> None:
    assert is_safe_corpus_identifier(bad) is False


def test_is_safe_corpus_identifier_accepts_conservative_names() -> None:
    assert is_safe_corpus_identifier("mtg-rules-corpus") is True
    assert is_safe_corpus_identifier("corpus.v1_2") is True


def test_unsafe_corpus_identifier_never_stats_outside_the_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin_store

    component_dir = tmp_path / "deep" / "component"
    root = _write_component(component_dir, dependencies=[
        {"kind": "corpus/data", "identifier": "../../canary", "digest": "sha256:" + "9" * 64},
    ])
    # A real directory at the escape target — pre-fix, `component_dir /
    # "corpus" / "../../canary"` resolves here and gets content-hashed.
    canary = tmp_path / "deep" / "canary"
    canary.mkdir(parents=True)
    (canary / "secret").write_text("x", encoding="utf-8")

    called: list = []
    real_cc = plugin_store.content_checksum

    def _tracking_cc(path):
        called.append(str(path))
        return real_cc(path)

    monkeypatch.setattr(plugin_store, "content_checksum", _tracking_cc)

    component = load_specialist_component(root, root / "manifest.json")
    rows = [r for r in resolve_dependency_closure(component, root) if r.kind == "corpus/data"]
    assert rows[0].available is False
    assert "unsafe corpus identifier" in rows[0].detail
    # RED on pre-fix: content_checksum was invoked on the escaping path.
    assert not any("canary" in c for c in called)


# --------------------------------------------------------------------------
# F5 — fail-closed self-heal on op-file / tuple divergence
# --------------------------------------------------------------------------


def _install_active(tmp_path: Path, slug: str = "mtg"):
    inspection, acks = _approved_inspection(tmp_path, slug=slug)
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert instance.state == "active"
    return specialists_dir, agents_dir


def test_self_heal_drops_stale_slug_when_rematerialize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import specialist_materialize

    specialists_dir, agents_dir = _install_active(tmp_path)
    slug_dir = agents_dir / "mtg"
    assert slug_dir.is_symlink()

    # Corrupt the on-disk binding marker so it no longer matches the active
    # tuple — simulating op-files left over from a superseded tuple.
    content = agents_dir / os.readlink(slug_dir)
    (content / ".binding-digest").write_text(
        json.dumps({"binding_digest": "STALE", "root": "STALE"}), encoding="utf-8")

    # Force every subsequent re-materialize to fail.
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(specialist_materialize, "_write_specialist_operational_files", _boom)

    specialist_materialize.current_specialist_roles_dir(
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # RED on pre-fix: the stale symlink survived (only a warning was logged),
    # leaving the slug running with stale capabilities.
    assert not (agents_dir / "mtg").exists()
    assert not (agents_dir / "mtg").is_symlink()


def test_self_heal_keeps_current_slug_when_rematerialize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import specialist_materialize

    specialists_dir, agents_dir = _install_active(tmp_path)
    slug_dir = agents_dir / "mtg"
    # Marker is CURRENT for the active tuple (written at install) — a
    # re-materialize failure here is benign, the files already match.

    def _boom(*a, **k):
        raise RuntimeError("transient")

    monkeypatch.setattr(specialist_materialize, "_write_specialist_operational_files", _boom)

    specialist_materialize.current_specialist_roles_dir(
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # A current marker means the op-files are kept; the slug stays loadable.
    assert (agents_dir / "mtg").is_symlink()
    assert (agents_dir / "mtg" / "runtime.yaml").is_file()


# --------------------------------------------------------------------------
# F6 — rollback verification gate
# --------------------------------------------------------------------------


def test_rollback_refuses_when_prior_dependency_now_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")
    upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    from personality_binding import InstanceDir
    active_before = InstanceDir(specialists_dir / "mtg").active()
    assert active_before is not None

    # Make the dependency closure report an unavailable dep at rollback time.
    from specialist_install import DependencyResolution

    def _unavailable(component, component_dir):
        return (DependencyResolution(
            kind="plugin/implementation", identifier="mtg", digest="sha256:" + "9" * 64,
            available=False, detail="plugin no longer registered"),)

    monkeypatch.setattr(specialist_install, "resolve_dependency_closure", _unavailable)

    with pytest.raises(SpecialistInstallError) as exc:
        rollback_specialist(
            slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert exc.value.kind == "dependency_unavailable"

    # RED on pre-fix: rollback ignored the closure entirely and committed the
    # prior tuple, swapping the running active out from under a broken dep.
    still_active = InstanceDir(specialists_dir / "mtg").active()
    assert still_active is not None
    assert still_active.binding.binding_digest == active_before.binding.binding_digest


# --------------------------------------------------------------------------
# Md — concurrent-install CAS race
# --------------------------------------------------------------------------


def test_commit_survives_concurrent_cas_publish_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection, acks = _approved_inspection(tmp_path, slug="mtg")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    cas_dir = specialist_install.cas_store_dir(
        inspection.root_digest, store_root=specialists_dir / "store")

    real_replace = os.replace
    raced = {"done": False}

    def _racing_replace(src, dst):
        # Simulate a concurrent install winning the race for THIS exact CAS
        # dir: publish identical bytes first, so our own os.replace then hits a
        # now-non-empty target and raises ENOTEMPTY.
        if not raced["done"] and Path(dst) == cas_dir and Path(src).exists():
            raced["done"] = True
            shutil.copytree(src, dst)
        return real_replace(src, dst)

    monkeypatch.setattr(specialist_install.os, "replace", _racing_replace)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # RED on pre-fix: the bare os.replace raised OSError uncaught. Post-fix the
    # race is absorbed and the install re-verifies the winner's bytes.
    assert raced["done"] is True
    assert instance.state == "active"
