"""init-plugin-store oneshot (plugin_boot): bundled import → seed → migration
(sentinel-guarded) → resolve-all → health report. ALWAYS returns 0 (§3.6);
migration issues reach the health report (§3.10)."""
from __future__ import annotations

import pytest

import plugin_boot
import plugin_health
import plugin_migration
import plugin_registry
import plugin_store
from plugin_registry import PluginIssue, RegistryData, ResolutionResult

pytestmark = pytest.mark.unit


def _wire(monkeypatch, tmp_path, *, valid=True, import_issues=None,
          seeded=False, migration=None, resolve_issues=None):
    reports = {}
    monkeypatch.setattr(plugin_boot, "BUNDLE_ROOT", tmp_path / "bundle")
    monkeypatch.setattr(plugin_boot, "SENTINEL", tmp_path / ".migration-done")
    monkeypatch.setattr(plugin_registry, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(plugin_store, "import_bundle",
                        lambda root: list(import_issues or []))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: RegistryData(
                            raw={"schema_version": 1, "seeded_defaults": [],
                                 "plugins": []}, valid=valid))
    monkeypatch.setattr(plugin_registry, "seed_defaults", lambda *a, **k: seeded)
    monkeypatch.setattr(plugin_registry, "save_registry", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: ResolutionResult(registry_valid=valid,
                                                 issues=list(resolve_issues or [])))
    if migration is not None:
        monkeypatch.setattr(plugin_migration, "run_migration", migration)

    def _write(*, issues, warnings, path=None):
        reports["issues"] = list(issues)
        reports["warnings"] = list(warnings)
    monkeypatch.setattr(plugin_health, "write_report", _write)
    return reports


def test_boot_happy_returns_zero(monkeypatch, tmp_path):
    (tmp_path / ".migration-done").touch()          # sentinel present → no migration
    reports = _wire(monkeypatch, tmp_path)
    assert plugin_boot.main() == 0
    assert "issues" in reports                       # health report written


def test_boot_unreadable_registry_reports_invalid(monkeypatch, tmp_path):
    (tmp_path / ".migration-done").touch()
    reports = _wire(monkeypatch, tmp_path, valid=False)
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "registry_invalid" for i in reports["issues"])


def test_migration_only_when_sentinel_absent(monkeypatch, tmp_path):
    calls = {"n": 0}

    def _mig(**kw):
        calls["n"] += 1
        return ({"migrated": [], "issues": []}, [], [])

    # Sentinel absent → migration runs.
    _wire(monkeypatch, tmp_path, migration=_mig)
    plugin_boot.main()
    assert calls["n"] == 1

    # Sentinel present → migration skipped.
    (tmp_path / ".migration-done").touch()
    calls["n"] = 0
    plugin_boot.main()
    assert calls["n"] == 0


def test_migration_issues_land_in_health(monkeypatch, tmp_path):
    issue = PluginIssue(name="lesina", target=None, stage="migration",
                        reason_code="ref_not_found")

    def _mig(**kw):
        return ({"migrated": [], "issues": [{"name": "lesina"}]}, [issue], [])

    reports = _wire(monkeypatch, tmp_path, migration=_mig)
    plugin_boot.main()
    assert any(i.reason_code == "ref_not_found" for i in reports["issues"])


def test_exploding_migration_returns_zero_with_boot_exception(monkeypatch, tmp_path):
    def _mig(**kw):
        raise RuntimeError("boom")

    reports = _wire(monkeypatch, tmp_path, migration=_mig)
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "boot_exception" for i in reports["issues"])
