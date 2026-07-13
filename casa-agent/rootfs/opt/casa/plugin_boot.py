"""init-plugin-store oneshot entry (spec 3.6).

Order: bundled-artifact import -> registry load/seed -> resolve-all validation
pass -> plugin-health report. EVERY failure is converted into a plugin-health
issue and the process exits 0 — Casa always starts, degraded per spec 3.5. A
nonzero exit is reserved for s6/process infrastructure failure (it would block
svc-casa).

The one-time pre-v0.71.0 legacy migration was removed in v0.72.0 (no pre-v0.71.0
Casa is installable any longer). Fresh-install seeding no longer depends on a
migration sentinel: ``seed_defaults`` is idempotent and the registry's permanent
``seeded_defaults`` ledger — not any boot flag — is what prevents resurrecting an
operator-removed default, so it runs unconditionally.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

BUNDLE_ROOT = Path("/opt/casa/plugin-bundle")


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

        # Seed the default registry. A MISSING registry.json loads as a valid,
        # empty in-memory document, so on a fresh install today's non-empty
        # bundled catalog makes seeding the write that CREATES registry.json; on
        # an existing install it only adds defaults introduced by a newer
        # release. seed_defaults never re-adds an operator-removed default (the
        # seeded_defaults ledger records every name ever seeded). A corrupt /
        # zero-byte registry loads as INVALID and is left untouched — never
        # overwritten as if fresh (which would destroy evidence and could reseed
        # removed defaults).
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
