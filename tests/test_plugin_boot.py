"""init-plugin-store oneshot (plugin_boot): bundled import → seed → resolve-all →
health report. ALWAYS returns 0 (§3.6). The pre-v0.71.0 migration was removed in
v0.72.0; seeding is now unconditional (no sentinel) and idempotent."""
from __future__ import annotations

import pytest

import plugin_boot
import plugin_health
import plugin_registry
import plugin_store
from plugin_registry import PluginIssue, RegistryData, ResolutionResult

pytestmark = pytest.mark.unit


def _wire(monkeypatch, tmp_path, *, valid=True, import_issues=None,
          seeded=False, resolve_issues=None):
    reports = {"saved": 0}
    monkeypatch.setattr(plugin_boot, "BUNDLE_ROOT", tmp_path / "bundle")
    monkeypatch.setattr(plugin_registry, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(plugin_store, "import_bundle",
                        lambda root: list(import_issues or []))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: RegistryData(
                            raw={"schema_version": 1, "seeded_defaults": [],
                                 "plugins": []}, valid=valid))
    monkeypatch.setattr(plugin_registry, "seed_defaults", lambda *a, **k: seeded)

    def _save(*a, **k):
        reports["saved"] += 1
    monkeypatch.setattr(plugin_registry, "save_registry", _save)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: ResolutionResult(registry_valid=valid,
                                                 issues=list(resolve_issues or [])))

    def _write(*, issues, warnings, path=None):
        reports["issues"] = list(issues)
        reports["warnings"] = list(warnings)
    monkeypatch.setattr(plugin_health, "write_report", _write)
    return reports


def test_boot_happy_returns_zero(monkeypatch, tmp_path):
    reports = _wire(monkeypatch, tmp_path)
    assert plugin_boot.main() == 0
    assert "issues" in reports                       # health report written


def test_boot_unreadable_registry_reports_invalid(monkeypatch, tmp_path):
    reports = _wire(monkeypatch, tmp_path, valid=False)
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "registry_invalid" for i in reports["issues"])


def test_boot_seeds_unconditionally_and_saves(monkeypatch, tmp_path):
    """v0.72.0: migration + its sentinel are gone — seed_defaults runs on EVERY
    boot (no sentinel gate) and a mutating seed is persisted. On a fresh install
    the absent registry loads valid-empty and this is the write that creates it."""
    reports = _wire(monkeypatch, tmp_path, seeded=True)    # seed reports a mutation
    assert plugin_boot.main() == 0
    assert reports["saved"] == 1


def test_boot_seed_noop_not_saved(monkeypatch, tmp_path):
    """A no-op seed (nothing new to add) does not rewrite the registry."""
    reports = _wire(monkeypatch, tmp_path, seeded=False)
    assert plugin_boot.main() == 0
    assert reports["saved"] == 0


def test_boot_invalid_registry_not_seeded_not_overwritten(monkeypatch, tmp_path):
    """A corrupt/zero-byte registry must NOT be treated as fresh: no seed, no
    save (never overwrite evidence / reseed removed defaults), flag invalid."""
    seen = {"seeded": False}
    reports = _wire(monkeypatch, tmp_path, valid=False)
    monkeypatch.setattr(
        plugin_registry, "seed_defaults",
        lambda *a, **k: seen.__setitem__("seeded", True) or True)
    assert plugin_boot.main() == 0
    assert seen["seeded"] is False and reports["saved"] == 0
    assert any(i.reason_code == "registry_invalid" for i in reports["issues"])


def test_boot_resolve_issues_reach_health(monkeypatch, tmp_path):
    issue = PluginIssue(name="lesina", target=None, stage="resolve",
                        reason_code="artifact_invalid")
    reports = _wire(monkeypatch, tmp_path, resolve_issues=[issue])
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "artifact_invalid" for i in reports["issues"])


def test_boot_exception_returns_zero_with_boot_exception(monkeypatch, tmp_path):
    """§3.6: any boot exception becomes a boot_exception health issue and the
    process still exits 0 (never blocks svc-casa)."""
    reports = _wire(monkeypatch, tmp_path)

    def _boom(root):
        raise RuntimeError("boom")
    monkeypatch.setattr(plugin_store, "import_bundle", _boom)
    assert plugin_boot.main() == 0
    assert any(i.reason_code == "boot_exception" for i in reports["issues"])
