"""Plan 2 review finding (no GH issue — found alongside GH #200): boot
(casa_core.py) constructs the process-wide ``InstalledSpecialistIndex`` and
publishes it via ``specialist_registry.set_active_installed_index``, but every
install/upgrade/rollback/uninstall + specialist-tier reload re-scan used to
build a FRESH local index (inside ``specialist_materialize.
current_specialist_roles_dir``, via ``reload.py``'s ``_specialist_roles_dir``
helper) and never republish it — so ``live_installed_specialist_slugs()`` /
``live_collision_slugs()`` / ``get_installed_instance()`` (admin/inspection,
Task-14 handlers) served BOOT-TIME state forever, never seeing a specialist
installed/upgraded/rolled-back/uninstalled after boot.

Fixed by having ``reload._specialist_roles_dir`` build the index itself
(mirroring casa_core.py's own boot sequence) and call
``set_active_installed_index`` before handing it to
``current_specialist_roles_dir``.

Autouse fixture saves/restores ``specialist_registry._active_index`` around
every test in THIS file (not repo-wide, not conftest.py — this is the only
file that drives a REAL, non-mock refresh of that global via a genuine
reload path) so it can never leak into other test files."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_active_specialist_index():
    import specialist_registry as specialist_registry_mod

    original = specialist_registry_mod._active_index
    yield
    specialist_registry_mod._active_index = original


def _install_specialist(tmp_path: Path, *, slug: str) -> tuple[Path, Path]:
    """Commits a real, fully-installed specialist under a tmp_path tree —
    the same dance test_reload_specialist_roles_overlay.py uses — so this
    test exercises the genuine InstalledSpecialistIndex.load() path rather
    than a hand-rolled active.yaml."""
    from specialist_component import load_specialist_component
    from specialist_install import (
        InspectionResult, commit_specialist_install,
        compute_install_root_digest, resolve_dependency_closure,
    )
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
    from test_specialist_install import _write_component

    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "config" / "agents" / "specialists"
    staged = _write_component(tmp_path / "staged", slug=slug)
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
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
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)
    commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    return specialists_dir, agents_specialists_dir


def test_specialist_tier_reload_refreshes_the_process_wide_installed_index(tmp_path):
    import specialist_registry as specialist_registry_mod
    from reload import _specialist_roles_dir

    specialists_dir, agents_specialists_dir = _install_specialist(tmp_path, slug="gizmo")

    # Simulate stale boot-time state: an index that predates the install
    # above (e.g. boot happened before "gizmo" was ever installed).
    specialist_registry_mod.set_active_installed_index(
        specialist_registry_mod.InstalledSpecialistIndex(str(tmp_path / "boot-time-empty")))
    assert "gizmo" not in specialist_registry_mod.live_installed_specialist_slugs()

    runtime = SimpleNamespace(
        config_dir=str(specialists_dir.parent),
        agents_dir=str(agents_specialists_dir.parent),
    )
    # Every specialist-tier reload call site funnels through this ONE helper.
    _specialist_roles_dir(runtime)

    assert "gizmo" in specialist_registry_mod.live_installed_specialist_slugs()
    assert specialist_registry_mod.get_installed_instance("gizmo") is not None


def test_leaked_index_from_a_reload_would_be_visible_without_the_fixture_restore():
    """Sanity check for the fixture itself: mutate the global directly (no
    reload involved) so the NEXT test can prove the autouse fixture actually
    restored the pristine value on unwind, not just left it around."""
    import specialist_registry as specialist_registry_mod

    specialist_registry_mod.set_active_installed_index(
        specialist_registry_mod.InstalledSpecialistIndex(str(Path("/nonexistent-marker-for-test"))))
    assert specialist_registry_mod._active_index is not None
    assert specialist_registry_mod._active_index._dir == Path("/nonexistent-marker-for-test")


def test_save_restore_keeps_the_next_test_clean():
    """Proves the autouse fixture undid the previous test's mutation: if it
    had leaked, `_active_index` here would still be the marker index the
    prior test installed."""
    import specialist_registry as specialist_registry_mod

    current_dir = getattr(specialist_registry_mod._active_index, "_dir", None)
    assert current_dir != Path("/nonexistent-marker-for-test")
