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

        # Seed AFTER migration: a no-op on the first boot (migration already
        # considered every bundled default and marked seeded_defaults), active
        # only for defaults introduced by a LATER release — no-resurrection
        # honored so a removed default stays removed across upgrades.
        data = plugin_registry.load_registry()
        if not data.valid:
            issues.append(plugin_registry.PluginIssue(
                name="*", target=None, stage="registry",
                reason_code="registry_invalid"))
        elif plugin_registry.seed_defaults(data):
            plugin_registry.save_registry(data)

        plugin_registry.reload_snapshot()
        res = plugin_registry.resolve_all()
        if not res.registry_valid:
            issues.append(plugin_registry.PluginIssue(
                name="*", target=None, stage="registry",
                reason_code="registry_invalid"))
        issues.extend(res.issues)
        warnings.extend(res.warnings)
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
