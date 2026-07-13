"""One-time migration to the unified plugin architecture (spec §3.7).

Runs on the first boot of v0.71.0 (guarded by a sentinel). Builds the
registry from the LEGACY installed state — driven by installed state, never
version-dir guessing (choosing "highest" would repeat the incident). Offline-
safe (offline-adopt when GitHub is unreachable), and durable: the report is
written+fsynced FIRST, the sentinel created LAST, so a crash between the two
re-runs safely (content-addressed publishes converge).

NEVER raises: every failure is caught into a report issue. Returns
``(report, issues, warnings)`` — plugin_boot merges the issues/warnings into
the plugin-health report so migration failures reach the operator DM (§3.10).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
from pathlib import Path

import plugin_store
from plugin_registry import (
    REGISTRY_PATH, STORE_ROOT, PluginIssue, RegistryData,
    load_registry, save_registry,
)
from plugin_store import STAGING_ROOT

logger = logging.getLogger(__name__)

SENTINEL_PATH = Path("/config/plugins/.migration-done")
REPORT_PATH = Path("/data/plugin-migration-report.json")


def _parse_installed_rows(rows) -> dict:
    """Normalize installed-state from EITHER the CLI (`claude plugin list
    --json`: a list of {id|name, installPath, scope, projectPath, enabled}) OR
    the fallback installed_plugins.json (a dict). CLI rows expose
    ``id: "name@marketplace"``, not a bare ``name`` (Sol F9). Returns
    ``name -> {installPaths: set, scopes: set, enabled: bool}``."""
    out: dict = {}

    def _row(name, install_path, scope, enabled):
        if not name:
            return
        rec = out.setdefault(name, {"installPaths": set(), "scopes": set(),
                                    "enabled": False})
        if install_path:
            rec["installPaths"].add(install_path)
        if scope:
            rec["scopes"].add(scope)
        rec["enabled"] = rec["enabled"] or bool(enabled)

    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = r.get("name") or str(r.get("id", "")).partition("@")[0]
            _row(name, r.get("installPath"), r.get("scope"), r.get("enabled"))
    elif isinstance(rows, dict):
        # Real v2 installed_plugins.json (CC 2.1.x, verified against the 2.1.207
        # binary): {"version":2,"plugins":{"<name>@<marketplace>":[<records>]}}.
        # The map lives under "plugins" and each value is a LIST of install
        # records (installPath/scope/projectPath); the plugin name is ONLY in
        # the "<name>@<marketplace>" key — records carry no name/enabled. Unwrap
        # the versioned envelope; else treat rows as a flat {name: record} dict
        # (older/hand-written fallback).
        if "version" in rows and isinstance(rows.get("plugins"), dict):
            inner = rows["plugins"]
        else:
            inner = rows
        for key, val in inner.items():
            key_name = str(key).partition("@")[0]
            if isinstance(val, list):                 # v2: key -> [records]
                for rec in val:
                    if isinstance(rec, dict):
                        _row(key_name, rec.get("installPath"), rec.get("scope"),
                             rec.get("enabled", True))
            elif isinstance(val, dict):               # flat legacy: name -> record
                nm = (val.get("name")
                      or str(val.get("id", "")).partition("@")[0] or key_name)
                _row(nm, val.get("installPath"), val.get("scope"),
                     val.get("enabled", True))
    return out


def _installed_state(cc_home: Path) -> dict:
    env = {**os.environ, "HOME": str(cc_home),
           "CLAUDE_CODE_PLUGIN_CACHE_DIR": str(Path(cc_home) / ".claude" / "plugins")}
    try:
        proc = subprocess.run(["claude", "plugin", "list", "--json"],
                              env=env, capture_output=True, text=True,
                              timeout=30, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return _parse_installed_rows(json.loads(proc.stdout))
    except Exception as exc:  # noqa: BLE001
        logger.warning("migration: `claude plugin list` failed: %s", exc)
    try:
        raw = json.loads((Path(cc_home) / ".claude" / "plugins"
                          / "installed_plugins.json").read_text(encoding="utf-8"))
        return _parse_installed_rows(raw)
    except (OSError, ValueError):
        return {}


def _load_user_marketplace(config_dir: Path) -> dict:
    """name -> {repo, ref} from the legacy user marketplace manifest."""
    try:
        doc = json.loads((Path(config_dir) / "marketplace" / ".claude-plugin"
                          / "marketplace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for p in doc.get("plugins", []):
        if not isinstance(p, dict):
            continue
        src = p.get("source") or {}
        out[p.get("name")] = {"repo": src.get("repo", ""),
                              "ref": src.get("ref", "")}
    return out


def _load_default_registry(defaults_dir: Path) -> dict:
    try:
        doc = json.loads((Path(defaults_dir) / "plugin-registry.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {e["name"]: e for e in doc.get("plugins", [])
            if isinstance(e, dict) and e.get("name")}


def _offline_revision(install_path: Path, repo: str) -> tuple[str, bool]:
    """Determine the offline-adopt revision. Returns (revision, is_legacy).
    A clean checkout whose HEAD matches its remote → git:<HEAD>; a dirty tree,
    missing .git, or remote mismatch → ('', True) meaning caller must use
    publish_legacy_tree (content checksum)."""
    def _git(*args):
        return subprocess.run(["git", "-C", str(install_path), *args],
                              capture_output=True, text=True, timeout=15,
                              check=False)
    if not (Path(install_path) / ".git").is_dir():
        return "", True
    try:
        head = _git("rev-parse", "HEAD")
        status = _git("status", "--porcelain")
        origin = _git("config", "--get", "remote.origin.url")
    except Exception:  # noqa: BLE001
        return "", True
    if head.returncode != 0 or status.stdout.strip():
        return "", True                       # no HEAD or dirty
    commit = head.stdout.strip()
    remote = origin.stdout.strip().lower()
    # remote-match: the marketplace repo owner/name appears in origin url.
    if repo and repo.lower() in remote and len(commit) == 40:
        return f"git:{commit}", False
    return "", True


def _atomic_touch(path: Path) -> None:
    """Atomically + durably create the sentinel (Sol F9): temp file → fsync →
    os.replace → fsync directory. A bare Path.touch is neither atomic nor
    durable, and the report's durability guarantee depends on this ordering."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _write_report_atomic(report_path: Path, report: dict) -> None:
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.parent / (report_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, report_path)
    dfd = os.open(str(report_path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def run_migration(
    *,
    cc_home: Path = Path("/config/cc-home"),
    config_dir: Path = Path("/config"),
    data_dir: Path = Path("/data"),
    defaults_dir: Path = Path("/opt/casa/defaults"),
    agents_dir: Path = Path("/config/agents"),
    agent_home_root: Path = Path("/config/agent-home"),
    store_root: Path = STORE_ROOT,
    staging_root: Path = STAGING_ROOT,
    registry_path: Path = REGISTRY_PATH,
    sentinel_path: Path = SENTINEL_PATH,
    report_path: Path = REPORT_PATH,
) -> tuple[dict, list, list]:
    report = {"schema_version": 1, "migrated": [], "issues": [], "warnings": [],
              "user_scope_dropped": [], "divergence_from_default": []}
    issues: list = []
    warnings: list = []

    def add_issue(name, reason, target=None, artifact_id=None):
        issues.append(PluginIssue(name=str(name), target=target,
                                  stage="migration", reason_code=reason,
                                  artifact_id=artifact_id))
        report["issues"].append({"name": name, "reason_code": reason,
                                 "target": target})

    def add_warning(name, reason, target=None, artifact_id=None):
        warnings.append(PluginIssue(name=str(name), target=target,
                                    stage="migration", reason_code=reason,
                                    artifact_id=artifact_id))
        report["warnings"].append({"name": name, "reason_code": reason,
                                   "target": target})

    # Guard: an existing-but-unreadable registry is NEVER overwritten (Sol F9).
    if Path(registry_path).is_file():
        existing = load_registry(registry_path)
        if not existing.valid:
            add_issue("*", "registry_invalid_migration_skipped")
            _write_report_atomic(report_path, report)   # NO sentinel — retry later
            return report, issues, warnings
        data = existing
    else:
        data = RegistryData(raw={"schema_version": 1, "seeded_defaults": [],
                                 "plugins": []})

    migration_ok = True
    try:
        _migrate(data, cc_home, config_dir, defaults_dir, agents_dir,
                 agent_home_root, store_root, staging_root, report,
                 add_issue, add_warning)
        save_registry(data, registry_path)
        _config_git_untrack(config_dir, report, add_issue)
    except Exception as exc:  # noqa: BLE001 — never raise (spec 3.7)
        migration_ok = False
        logger.exception("migration failed: %s", exc)
        add_issue("*", "migration_exception")

    # Report FIRST (fsynced), sentinel LAST (§3.7 cross-filesystem ordering).
    # The sentinel is written ONLY when migration completed without a WHOLESALE
    # exception (Sol #3): a mid-flight crash leaves `data` UNSAVED (save_registry
    # is inside the try), so writing the sentinel would permanently skip an
    # un-migrated registry with no retry. Withholding it re-runs next boot —
    # safe, since content-addressed publishes converge. Per-plugin issues that
    # completed (add_issue, migration_ok stays True) still write the sentinel.
    _write_report_atomic(report_path, report)
    if migration_ok:
        try:
            _atomic_touch(sentinel_path)
        except OSError as exc:
            logger.warning("migration: sentinel write failed (will re-run): %s", exc)
    else:
        logger.warning(
            "migration incomplete (wholesale exception) — sentinel withheld, "
            "will retry next boot")
    return report, issues, warnings


def _migrate(data, cc_home, config_dir, defaults_dir, agents_dir,
             agent_home_root, store_root, staging_root, report,
             add_issue, add_warning):
    active = _installed_state(Path(cc_home))
    user_mkt = _load_user_marketplace(Path(config_dir))
    default_reg = _load_default_registry(Path(defaults_dir))
    existing_names = {e.get("name") for e in data.raw.get("plugins", [])
                      if isinstance(e, dict)}

    # entries being built: name -> registry entry dict
    new_entries: dict[str, dict] = {}

    def _publish_offline_or_online(name, repo, ref, subdir=""):
        """Returns (entry-source-dict, origin) or (None, reason)."""
        try:
            res = plugin_store.publish(name=name, repo=repo, ref=ref,
                                       subdir=subdir, store_root=store_root,
                                       staging_root=staging_root)
            return res, "github"
        except plugin_store.RefNotFound:
            return None, "ref_not_found"
        except plugin_store.ResolveUnavailable:
            pass  # fall through to offline-adopt
        except plugin_store.StoreError as exc:
            return None, getattr(exc, "reason_code", "store_error")
        # offline-adopt from the ACTIVE installPath
        paths = sorted(active.get(name, {}).get("installPaths", set()))
        if not paths or not Path(paths[0]).is_dir():
            return None, "artifact_unavailable"
        install_path = Path(paths[0])
        revision, is_legacy = _offline_revision(install_path, repo)
        try:
            if is_legacy:
                res = plugin_store.publish_legacy_tree(
                    name=name, repo=repo, ref=ref, subdir=subdir,
                    src_root=install_path, store_root=store_root,
                    staging_root=staging_root)
                add_warning(name, "legacy_provenance")
                return res, "offline_adopt"
            res = plugin_store.publish_from_tree(
                name=name, repo=repo, ref=ref, revision=revision, subdir=subdir,
                src_root=install_path, store_root=store_root,
                staging_root=staging_root)
            return res, "offline_adopt"
        except plugin_store.StoreError as exc:
            return None, getattr(exc, "reason_code", "store_error")

    # (a) bundled defaults — artifacts already imported by plugin_boot; create
    #     entries, honoring §3.7.3 active-installPath-wins precedence.
    for name, entry in default_reg.items():
        if name in existing_names:
            continue
        src = entry["source"]
        active_paths = sorted(active.get(name, {}).get("installPaths", set()))
        if len(active_paths) > 1:
            # Sol round-3 H10b: ambiguous — several installs of one bundled
            # default. REFUSE (leave unassigned) rather than silently fall back
            # to the bundled pin: the migration issue is not replayed after the
            # sentinel, so a bundled-pin fallback would verify green next boot
            # with the operator's divergence forgotten. Absence is the persistent
            # signal; the operator re-adds via plugin_add once resolved.
            add_issue(name, "install_path_divergence")
            continue
        if not (active_paths and Path(active_paths[0]).is_dir()):
            new_entries[name] = copy.deepcopy(entry)  # bundled pin verbatim
            continue
        install_path = Path(active_paths[0])
        revision, is_legacy = _offline_revision(install_path, src.get("repo", ""))
        if not is_legacy and revision == src.get("revision"):
            new_entries[name] = copy.deepcopy(entry)  # identical → bundled pin
            continue
        # Diverged: the operator was running a different build — adopt it.
        try:
            if is_legacy:
                res = plugin_store.publish_legacy_tree(
                    name=name, repo=src.get("repo", ""), ref=src.get("ref", ""),
                    subdir=src.get("subdir", ""), src_root=install_path,
                    store_root=store_root, staging_root=staging_root)
                add_warning(name, "legacy_provenance")
            else:
                res = plugin_store.publish_from_tree(
                    name=name, repo=src.get("repo", ""), ref=src.get("ref", ""),
                    revision=revision, subdir=src.get("subdir", ""),
                    src_root=install_path, store_root=store_root,
                    staging_root=staging_root)
            new_entries[name] = _entry_from_result(name, src, res)
            report["divergence_from_default"].append(
                {"name": name, "artifact_id": res.artifact_id})
        except plugin_store.StoreError:
            new_entries[name] = copy.deepcopy(entry)  # adopt failed → bundled pin

    # (b) user-marketplace plugins active but not a default.
    for name in active:
        if name in default_reg or name in existing_names or name in new_entries:
            continue
        # Sol #10: multiple distinct installPaths for one name → we cannot know
        # which build the operator intended. Refuse to adopt an arbitrary
        # (sorted-first) one; record the divergence and leave the plugin
        # unassigned/not-ready so the operator resolves it explicitly.
        if len(active[name]["installPaths"]) > 1:
            add_issue(name, "install_path_divergence")
            continue
        if name in user_mkt:
            repo = user_mkt[name]["repo"]
            ref = user_mkt[name]["ref"]
            res, origin = _publish_offline_or_online(name, repo, ref)
            if res is None:
                add_issue(name, origin)
            else:
                new_entries[name] = _entry_from_result(
                    name, {"repo": repo, "ref": ref, "subdir": ""}, res)
        else:
            # user-scope CLI plugin, not in any pin source → dropped loudly.
            report["user_scope_dropped"].append(name)

    # (c) assignments from agent-homes + executor plugins.yaml.
    _assign_targets(new_entries, agents_dir, agent_home_root, default_reg,
                    report, add_issue)

    # write entries + seeded_defaults (all default names considered).
    data.raw.setdefault("plugins", [])
    # Sol #1 defense-in-depth: never append a name already present. The reorder
    # (migration-before-seed) means `present` is empty on a first-boot migration,
    # but this keeps the append idempotent against any pre-populated registry.
    present = {e.get("name") for e in data.raw["plugins"] if isinstance(e, dict)}
    for entry in new_entries.values():
        if entry["name"] in present:
            continue
        if entry.get("targets"):                # only keep assigned plugins
            data.raw["plugins"].append(entry)
            present.add(entry["name"])
            report["migrated"].append({
                "name": entry["name"], "artifact_id": entry["artifact_id"],
                "revision": entry["source"]["revision"],
                "version": entry.get("version"), "targets": entry["targets"],
                "origin": entry.pop("_origin", "bundled")})
    seeded = data.raw.setdefault("seeded_defaults", [])
    for name in default_reg:
        if name not in seeded:
            seeded.append(name)


def _entry_from_result(name, src, res) -> dict:
    return {
        "name": name,
        "source": {"type": "github", "repo": src.get("repo", ""),
                   "ref": src.get("ref", ""), "revision": res.revision,
                   "subdir": src.get("subdir", "")},
        "artifact_id": res.artifact_id, "version": res.version,
        "targets": [], "_origin": "offline_adopt"
        if res.revision.startswith("legacy-content:") else "github",
    }


def _assign_targets(new_entries, agents_dir, agent_home_root, default_reg,
                    report, add_issue):
    agents_dir = Path(agents_dir)
    suppressed: set[tuple[str, str]] = set()   # (name, target) disabled

    def _ensure(name):
        if name not in new_entries and name in default_reg:
            new_entries[name] = copy.deepcopy(default_reg[name])
        return new_entries.get(name)

    def _add_target(name, target):
        if (name, target) in suppressed:
            return
        entry = _ensure(name)
        if entry is not None and target not in entry.setdefault("targets", []):
            entry["targets"].append(target)

    # Residents + specialists — from each agent-home's enabledPlugins.
    if agents_dir.is_dir():
        for role_dir in agents_dir.iterdir():
            if not role_dir.is_dir():
                continue
            if role_dir.name == "specialists":
                for sdir in role_dir.iterdir():
                    if sdir.is_dir():
                        _apply_enabled(sdir.name, "specialist", agent_home_root,
                                       suppressed, _add_target, add_issue)
            elif role_dir.name == "executors":
                for edir in role_dir.iterdir():
                    if edir.is_dir():
                        _apply_executor(edir, default_reg, new_entries,
                                        suppressed)
            else:
                _apply_enabled(role_dir.name, "resident", agent_home_root,
                               suppressed, _add_target, add_issue)


def _apply_enabled(role, tier, agent_home_root, suppressed, add_target,
                   add_issue):
    settings = (Path(agent_home_root) / role / ".claude" / "settings.json")
    try:
        enabled = json.loads(settings.read_text(encoding="utf-8")).get(
            "enabledPlugins") or {}
    except (OSError, ValueError):
        enabled = {}
    # Sol #3: `enabledPlugins` MUST be an object. A malformed non-dict (e.g. a
    # hand-edited list) would raise AttributeError on `.items()` below and, via
    # the wholesale except in run_migration, permanently disable migration.
    # Record a scoped issue and skip this role instead.
    if not isinstance(enabled, dict):
        add_issue(role, "enabled_plugins_malformed", target=f"{tier}:{role}")
        enabled = {}
    for key, on in enabled.items():
        name = str(key).partition("@")[0]
        target = f"{tier}:{role}"
        if not on:
            suppressed.add((name, target))       # §3.7.3 disablement wins
        else:
            add_target(name, target)


def _apply_executor(edir, default_reg, new_entries, suppressed):
    import copy
    import yaml
    etype = edir.name
    target = f"executor:{etype}"
    plugins_yaml = edir / "plugins.yaml"
    if not plugins_yaml.is_file():
        return   # no override → the bundled-default targets apply unchanged
    # Authoritative: only listed names get the target; every omitted default
    # has this executor target REMOVED (not merely blocked from re-add).
    try:
        listed = {p.get("name") for p in (yaml.safe_load(
            plugins_yaml.read_text(encoding="utf-8")) or {}).get("plugins", [])
            if isinstance(p, dict)}
    except Exception:  # noqa: BLE001
        listed = set()
    for name in default_reg:
        if name not in listed:
            suppressed.add((name, target))
            ent = new_entries.get(name)
            if ent is not None and target in ent.get("targets", []):
                ent["targets"] = [t for t in ent["targets"] if t != target]
    for name in listed:
        entry = new_entries.get(name)
        if entry is None and name in default_reg:
            entry = copy.deepcopy(default_reg[name])
            new_entries[name] = entry
        if entry is not None and target not in entry.setdefault("targets", []):
            entry["targets"].append(target)


def _config_git_untrack(config_dir, report, add_issue):
    try:
        subprocess.run(
            ["git", "-C", str(config_dir), "rm", "--cached", "--ignore-unmatch",
             "marketplace/.claude-plugin/marketplace.json"],
            capture_output=True, text=True, timeout=15, check=False)
        import config_git
        config_git.commit_config(
            str(config_dir),
            "plugin migration: registry init, marketplace untracked")
    except Exception as exc:  # noqa: BLE001
        logger.warning("migration: config-git untrack failed: %s", exc)
        add_issue("*", "config_git_untrack_failed")
