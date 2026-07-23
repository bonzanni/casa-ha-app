"""Task 8: sourced plugin-dependency resolution, the seven-step validation
order, the trusted source receipt, and the new InspectionResult fields
(spec §1, §3.2.1)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import specialist_install
import specialist_receipt
from specialist_component import load_specialist_component
from specialist_install import (
    SpecialistInstallError,
    inspect_specialist_repo,
    resolve_dependency_closure,
)
from specialist_registry import InstalledSpecialistIndex

try:
    from tests.specialist_fixtures import write_bundled_plugin, write_minimal_component
except ImportError:
    from specialist_fixtures import write_bundled_plugin, write_minimal_component

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_plugin_registry_snapshot(tmp_path):
    """`plugin_registry` keeps a process-global cached snapshot (`_snapshot`)
    that only lazily reloads when `None` — a stale snapshot left by an
    earlier test module (in the same pytest process) would otherwise leak
    into this module's `_manifest_name_collisions`/`_env_name_conflicts`
    real (non-monkeypatched) code paths. Point it at a fresh, empty,
    tmp_path-scoped registry before every test in this file for
    determinism — mirrors the explicit `reload_snapshot(...)` pattern every
    other plugin_registry-adjacent test module already uses."""
    import plugin_registry
    plugin_registry.reload_snapshot(registry_path=tmp_path / "registry.json",
                                    store_root=tmp_path / "store")
    yield


def _stub_resolve_and_fetch(component_root: Path):
    """Mirrors test_specialist_install.py's own stub: a monkeypatched
    `specialist_install.resolve_and_fetch` that just copies an already-built
    local component tree into `dest` and returns a fake 40-hex commit sha —
    never real network/git I/O."""

    def _stub(repo: str, ref: str, subdir: str, dest: Path, *, expected_revision: str | None = None) -> str:
        shutil.copytree(component_root, dest)
        return "a" * 40

    return _stub


def _add_dependency_row(manifest_path: Path, row: dict) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"].append(row)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _bundled_dep_row(identifier: str, digest: str, path: str) -> dict:
    return {
        "kind": "plugin/implementation", "identifier": identifier, "digest": digest,
        "source": {"type": "bundled", "path": path},
    }


def _inspect(tmp_path: Path, component_dir: Path, *, slug: str = "mtg-test",
             monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_dir))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    return inspect_specialist_repo(
        "org/repo", "main",
        staging_root=tmp_path / "staging",
        installed_index=index,
        receipts_dir=tmp_path / "receipts",
    )


# ---------------------------------------------------------------------------
# Happy path: bundled dep resolves, receipt round-trips.
# ---------------------------------------------------------------------------


def test_bundled_dep_resolves_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)

    assert result.slug == "mtg-test"
    assert len(result.plugin_resolutions) == 1
    row = result.plugin_resolutions[0]
    assert row.scoped_name == "mtg-test.mtg"
    assert row.manifest_name == "mtg"
    assert row.identifier == "mtg"
    assert row.content_digest == digest
    assert row.source_type == "bundled"
    assert result.receipt_id != ""
    assert result.receipt_digest != ""

    loaded = specialist_receipt.load(result.receipt_id, receipts_dir=tmp_path / "receipts")
    assert loaded is not None
    assert loaded.receipt_digest == result.receipt_digest
    assert loaded.slug == "mtg-test"
    assert len(loaded.plugins) == 1
    assert loaded.plugins[0].content_digest == digest


# ---------------------------------------------------------------------------
# Digest mismatch.
# ---------------------------------------------------------------------------


def test_bundled_digest_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    write_bundled_plugin(component_dir, "mtg")
    wrong_digest = "sha256:" + "9" * 64
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", wrong_digest, "plugins/mtg"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "dependency_unavailable"


# ---------------------------------------------------------------------------
# Prohibitions.
# ---------------------------------------------------------------------------


def test_bundled_triggers_prohibited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg", triggers="not-a-valid-shape")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "bundled_triggers_unsupported"


def test_bundled_sysreqs_prohibited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(
        component_dir, "mtg", sysreqs=[{"type": "apt", "package": "libx"}])
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "bundled_sysreqs_unsupported"


def test_env_name_collision_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    monkeypatch.setattr(specialist_install, "_env_name_conflicts",
                        lambda *a, **k: ["SOME_API_KEY"])

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "env_name_collision"


def test_manifest_name_collision_precheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    import plugin_registry

    fake_registry = plugin_registry.RegistryData(raw={}, entries=[{
        "name": "mtg", "targets": ["specialist:mtg-test"],
        "source": {"type": "github", "repo": "x/y", "ref": "v1",
                   "revision": "git:" + "a" * 40, "subdir": ""},
        "version": "1.0.0", "artifact_id": "irrelevant",
    }])
    monkeypatch.setattr(plugin_registry, "snapshot_registry", lambda: fake_registry)

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect(tmp_path, component_dir, monkeypatch=monkeypatch)
    assert exc.value.kind == "manifest_name_collision"


def test_symlink_escape_in_bundled_path_fails(tmp_path: Path) -> None:
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    digest = write_bundled_plugin(component_dir, "mtg")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    (component_dir / "plugins" / "mtg" / "escape").symlink_to(outside)
    _add_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    component = load_specialist_component(component_dir, manifest_path)
    resolutions = resolve_dependency_closure(component, component_dir)
    plugin_rows = [r for r in resolutions if r.kind == "plugin/implementation"]
    assert len(plugin_rows) == 1
    assert plugin_rows[0].available is False
    assert "unsafe_archive" in plugin_rows[0].detail


# ---------------------------------------------------------------------------
# Legacy sourceless dep — must keep working unchanged.
# ---------------------------------------------------------------------------


def test_sourceless_dep_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin_store import content_checksum

    installed_dir = tmp_path / "installed-artifact"
    installed_dir.mkdir()
    (installed_dir / "file.txt").write_text("hello", encoding="utf-8")
    digest = "sha256:" + content_checksum(installed_dir)

    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    _add_dependency_row(manifest_path, {
        "kind": "plugin/implementation", "identifier": "mtg", "digest": digest,
    })
    component = load_specialist_component(component_dir, manifest_path)

    import plugin_registry
    from plugin_registry import ResolutionResult, ResolvedPlugin

    monkeypatch.setattr(
        plugin_registry, "resolve_all",
        lambda: ResolutionResult(registry_valid=True, plugins=[
            ResolvedPlugin(name="mtg", artifact_id="x", path=str(installed_dir),
                           version="1.0.0", manifest={}),
        ]),
    )

    resolutions = resolve_dependency_closure(component, component_dir)
    plugin_rows = [r for r in resolutions if r.kind == "plugin/implementation"]
    assert len(plugin_rows) == 1
    assert plugin_rows[0].available is True
