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


def test_boot_runs_migration_before_seed(monkeypatch, tmp_path):
    """Sol #1: migration MUST run BEFORE seed_defaults. Seeding first would
    pre-populate every default, so migration's existing_names skips them all —
    divergent installs never adopted and a customized executor plugins.yaml
    duplicated (both copies then dropped → zero plugins resolve)."""
    order = []

    def _mig(**kw):
        order.append("migrate")
        (tmp_path / ".migration-done").touch()    # migration COMPLETED → sentinel
        return ({"migrated": [], "issues": []}, [], [])

    _wire(monkeypatch, tmp_path, migration=_mig)
    monkeypatch.setattr(plugin_registry, "seed_defaults",
                        lambda *a, **k: (order.append("seed"), False)[1])
    assert plugin_boot.main() == 0
    assert order == ["migrate", "seed"]           # migration precedes seeding


def test_boot_skips_seed_when_migration_did_not_complete(monkeypatch, tmp_path):
    """Sol round-3 B1: a migration that FAILED without writing the sentinel must
    NOT seed defaults — seeding poisons the next retry (existing_names would then
    skip active-install precedence + executor overrides)."""
    seeded = {"called": False}

    def _mig(**kw):
        # Ran, recorded a failure issue, but did NOT write the sentinel.
        return ({"migrated": [], "issues": [{"name": "*"}]},
                [PluginIssue(name="*", target=None, stage="migration",
                             reason_code="migration_exception")], [])

    _wire(monkeypatch, tmp_path, migration=_mig)
    monkeypatch.setattr(
        plugin_registry, "seed_defaults",
        lambda *a, **k: (seeded.__setitem__("called", True), False)[1])
    # Sentinel absent (migration did not complete).
    assert plugin_boot.main() == 0
    assert seeded["called"] is False              # seeding skipped


def test_unresolved_migration_issues_replayed(tmp_path, monkeypatch):
    """Sol round-4: an unresolved migration issue (plugin still absent from the
    registry) is replayed into health; a now-present plugin's issue is dropped."""
    import json
    from plugin_registry import RegistryData
    monkeypatch.setattr(plugin_boot, "MIGRATION_REPORT", tmp_path / "report.json")
    (tmp_path / "report.json").write_text(json.dumps({"issues": [
        {"name": "superpowers", "reason_code": "install_path_divergence",
         "target": None},
        {"name": "present-plugin", "reason_code": "x", "target": None}]}))
    data = RegistryData(raw={"schema_version": 1,
                             "plugins": [{"name": "present-plugin"}]}, valid=True)
    out = plugin_boot._unresolved_migration_issues(data)
    codes = [(i.name, i.reason_code) for i in out]
    assert ("superpowers", "install_path_divergence") in codes   # absent → replayed
    assert not any(i.name == "present-plugin" for i in out)      # present → dropped


def test_unresolved_migration_issues_reason_filter(tmp_path, monkeypatch):
    """Sol round-4/5: EVERY plugin-scoped failure replays (denylist), including
    StoreError reasons like manifest_invalid/unsafe_archive; only role/global-
    scoped reasons (enabled_plugins_malformed) are excluded."""
    import json
    from plugin_registry import RegistryData
    monkeypatch.setattr(plugin_boot, "MIGRATION_REPORT", tmp_path / "r.json")
    (tmp_path / "r.json").write_text(json.dumps({"issues": [
        {"name": "sp", "reason_code": "install_path_divergence", "target": None},
        {"name": "badplug", "reason_code": "manifest_invalid", "target": None},
        {"name": "evilplug", "reason_code": "unsafe_archive", "target": None},
        {"name": "assistant", "reason_code": "enabled_plugins_malformed",
         "target": "resident:assistant"}]}), encoding="utf-8")
    data = RegistryData(raw={"schema_version": 1, "plugins": []}, valid=True)
    codes = [i.reason_code
             for i in plugin_boot._unresolved_migration_issues(data)]
    assert "install_path_divergence" in codes
    assert "manifest_invalid" in codes            # plugin-scoped StoreError → replayed
    assert "unsafe_archive" in codes
    assert "enabled_plugins_malformed" not in codes   # role-keyed → excluded
