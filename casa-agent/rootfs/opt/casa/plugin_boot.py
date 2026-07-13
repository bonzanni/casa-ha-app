"""init-plugin-store oneshot entry (spec 3.6/3.7).

Order: bundled-artifact import -> registry load/seed -> one-time migration
(if sentinel absent) -> resolve-all validation pass -> plugin-health report.
EVERY failure is converted into a plugin-health issue and the process exits
0 — Casa always starts, degraded per spec 3.5. A nonzero exit is reserved
for s6/process infrastructure failure (it would block svc-casa)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

BUNDLE_ROOT = Path("/opt/casa/plugin-bundle")
SENTINEL = Path("/config/plugins/.migration-done")
MIGRATION_REPORT = Path("/data/plugin-migration-report.json")

# Sol round-4/5: a migration issue is replayed while its plugin is still absent
# from the registry. Use a DENYLIST (not an allowlist) so EVERY plugin-scoped
# failure replays — manifest_invalid, name_mismatch, apt_requirements_rejected,
# unsafe_archive, ref_not_found, adoption_failed, install_path_divergence, … —
# and only reasons keyed on a ROLE or the "*" global scope (which could never
# clear by adding the named plugin) are excluded. Future plugin-scoped reasons
# are covered automatically.
_NON_REPLAYABLE_MIGRATION_REASONS = frozenset({
    "enabled_plugins_malformed",            # keyed on a ROLE, not a plugin
    "migration_exception",                  # global (name "*", already excluded)
    "registry_invalid_migration_skipped",   # global
    "config_git_untrack_failed",            # global
})


def _unresolved_migration_issues(data) -> list:
    """Sol round-4: migration issues (install_path_divergence, adoption_failed,
    …) for plugins STILL absent from the registry — replayed into the health
    report every boot AND on every mutation so a refused/divergent default doesn't
    silently go green after the one-time migration's sentinel. Once the operator
    re-adds the plugin (plugin_add) it is present and the issue naturally drops.
    Restricted to plugin-presence-clearable reasons."""
    import json
    from plugin_registry import PluginIssue
    try:
        report = json.loads(MIGRATION_REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    present = {e.get("name")
               for e in (data.raw.get("plugins", []) if getattr(data, "valid", False) else [])
               if isinstance(e, dict)}
    out = []
    for i in report.get("issues", []):
        name = i.get("name")
        if (name and name != "*" and name not in present
                and i.get("reason_code") not in _NON_REPLAYABLE_MIGRATION_REASONS):
            out.append(PluginIssue(name=name, target=i.get("target"),
                                   stage="migration",
                                   reason_code=i.get("reason_code")))
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="[plugin-boot] %(levelname)s %(message)s")
    log = logging.getLogger("plugin_boot")
    issues, warnings = [], []
    try:
        import plugin_health
        import plugin_registry
        import plugin_store

        plugin_registry.STORE_ROOT.mkdir(parents=True, exist_ok=True)
        issues.extend(plugin_store.import_bundle(BUNDLE_ROOT))

        # §3.7: the one-time migration builds the registry from LEGACY installed
        # state and MUST run BEFORE seed_defaults (Sol #1). Seeding first would
        # pre-populate every default, so migration's existing_names would skip
        # them all — divergent installs never adopted and a customized executor
        # plugins.yaml duplicated (both copies then dropped by the duplicate-name
        # rule → zero plugins resolve). Migration persists registry.json + the
        # sentinel itself.
        if not SENTINEL.exists():
            import plugin_migration
            report, mig_issues, mig_warnings = plugin_migration.run_migration()
            issues.extend(mig_issues)          # Sol F9: migration failures
            warnings.extend(mig_warnings)      # reach the health DM (3.10)
            log.info("migration ran: %d migrated, %d issues",
                     len(report.get("migrated", [])),
                     len(mig_issues))

        # Seed AFTER migration, and ONLY once migration has COMPLETED (sentinel
        # present). Sol round-3 B1: a FAILED migration (sentinel withheld) must
        # NOT seed — seeding the bundled defaults would make the next retry see
        # those names as existing and skip active-install precedence + executor
        # assignment overrides. On the first success the sentinel is present and
        # seeding is a no-op (migration already created the defaults); on later
        # boots it fills defaults introduced by a newer release (no-resurrection).
        data = plugin_registry.load_registry()
        if not data.valid:
            issues.append(plugin_registry.PluginIssue(
                name="*", target=None, stage="registry",
                reason_code="registry_invalid"))
        elif SENTINEL.exists() and plugin_registry.seed_defaults(data):
            plugin_registry.save_registry(data)

        plugin_registry.reload_snapshot()
        res = plugin_registry.resolve_all()
        if not res.registry_valid:
            issues.append(plugin_registry.PluginIssue(
                name="*", target=None, stage="registry",
                reason_code="registry_invalid"))
        issues.extend(res.issues)
        warnings.extend(res.warnings)
        # Sol round-4: replay UNRESOLVED migration issues so a refused/divergent
        # plugin stays visible in health across boots (migration runs only once,
        # so its report is the only record). An issue counts as unresolved iff its
        # plugin is STILL absent from the registry — once the operator re-adds it
        # (plugin_add), the issue naturally drops.
        issues.extend(_unresolved_migration_issues(data))
        plugin_health.write_report(issues=issues, warnings=warnings)
    except Exception as exc:  # noqa: BLE001 — spec 3.6: never block svc-casa
        log.exception("plugin store boot degraded: %s", exc)
        try:
            import plugin_health
            from plugin_registry import PluginIssue
            issues.append(PluginIssue(name="*", target=None, stage="boot",
                                      reason_code="boot_exception"))
            plugin_health.write_report(issues=issues, warnings=warnings)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
