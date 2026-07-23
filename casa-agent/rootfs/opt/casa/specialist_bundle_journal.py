"""Bundle-op journal + boot reconciliation with quarantine semantics
(design spec §3.1).

Every specialist-bundle mutation (install/upgrade/rollback/uninstall, Task 10)
journals its FULL before-state to `<ops_dir>/<slug>.<opid>.json` BEFORE any
durable mutation — fsynced (file AND directory) so a crash mid-write never
leaves a torn journal that looks complete. `reconcile_boot` runs before the
plugin snapshot loads (the boot hook in `plugin_boot.py`) and restores
consistency from whatever it finds:

- an **in-progress** journal is rolled back from its captured before-state
  (registry entries, tuple/sidecar files, consent-ack records) then unlinked;
- a journal whose payload reached `state == "complete"` (crash between the
  complete-write and the unlink) is pruned WITHOUT rollback — the op already
  finished, undoing it would be the bug;
- a journal whose FILENAME parses but whose payload is corrupt, fails strict
  structural validation, OR whose rollback itself fails (e.g. a malformed ack
  record) is quarantined: that slug's owned registry entries are removed and
  the slug is flagged, then the journal is renamed `.quarantined` (never
  deleted — forensics);
- a journal whose filename does not even parse quarantines EVERY owned
  registry entry (deterministic worst case — there is no slug to trust).

This is degrade-and-boot, matching `plugin_boot`'s philosophy: one specialist
must not brick the house. `reconcile_boot` itself never raises; its caller
still wraps the call (belt-and-suspenders) per the boot-hook contract.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import plugin_registry

logger = logging.getLogger(__name__)

OPS_DIR = Path("/config/specialists/.ops")
SPECIALISTS_DIR = Path("/config/specialists")
ACKS_PATH = Path("/data/specialist_install_acks.json")

SCHEMA_VERSION = 1

# Slug encoded in the FILENAME (outside the corruptible payload) so a corrupt
# journal still identifies its slug for selective quarantine (Sol r4).
JOURNAL_NAME_RE = re.compile(
    r"^(?P<slug>[a-z0-9][a-z0-9-]{0,31})\.(?P<opid>[0-9a-f]{32})\.json$"
)

# The fixed set of bare filenames a bundle transaction may ever record in
# `before.tuple_files` — written ONLY under specialists_dir/<slug>/, never a
# caller- or payload-supplied path (containment against traversal).
TUPLE_FILENAMES = frozenset({
    "active.yaml", "desired.yaml", "active.prior.yaml",
    "owned-plugins.yaml", "owned-plugins.desired.yaml",
    "owned-plugins.prior.yaml",
})

# Task 11 reads this after every boot to surface reconciliation results in
# the plugin-health report.
last_boot_reconcile_actions: list[dict] = []


def _fsync_write(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def begin(op: str, slug: str, *, before_entries: list[dict],
          before_tuple_files: dict[str, "str | None"],
          ack_records: list[dict], receipt_digest: str = "",
          ops_dir: Path = OPS_DIR) -> Path:
    """Write `<slug>.<uuid4hex>.json` with the full before-state, fsynced
    (file AND directory). Returns the journal path."""
    ops_dir = Path(ops_dir)
    ops_dir.mkdir(parents=True, exist_ok=True)
    path = ops_dir / f"{slug}.{uuid.uuid4().hex}.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "op": op,
        "slug": slug,
        "state": "in-progress",
        "before": {
            "registry_entries": before_entries,
            "tuple_files": before_tuple_files,
            "ack_records": ack_records,
        },
        "receipt_digest": receipt_digest,
        "steps_done": [],
    }
    _fsync_write(path, _dump(payload))
    return path


def mark_step(journal_path: Path, step: str) -> None:
    journal_path = Path(journal_path)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload.setdefault("steps_done", []).append(step)
    _fsync_write(journal_path, _dump(payload))


def complete(journal_path: Path) -> None:
    """Mark the journal complete (fsynced) THEN unlink. A crash between the
    two leaves a `state == "complete"` file on disk — `reconcile_boot` prunes
    that without rolling back (the op already finished)."""
    journal_path = Path(journal_path)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["state"] = "complete"
    _fsync_write(journal_path, _dump(payload))
    journal_path.unlink()


@dataclass(frozen=True)
class BundleTxn:
    """The rollback half of a bundle transaction — reusable by boot
    reconciliation (below) and by Task 10's in-process compensation path."""

    journal_path: Path
    slug: str
    before_entries: list[dict]
    before_tuple_files: dict
    ack_records: list[dict]
    removed_artifact_ids: tuple[str, ...] = ()
    new_artifact_ids: tuple[str, ...] = ()
    registry_path: Path = plugin_registry.REGISTRY_PATH
    specialists_dir: Path = SPECIALISTS_DIR
    acks_path: Path = ACKS_PATH

    def rollback_disk(self) -> None:
        """Restore the registry entries, tuple/sidecar files, and consent-ack
        records captured in `before` to their recorded destinations. Sync —
        callers running in async contexts dispatch it to a thread."""
        from specialist_install_consent import SpecialistInstallAckStore

        # 1. Registry entries: drop anything currently owned by this slug,
        # reinsert the recorded before-entries, save.
        data = plugin_registry.load_registry(self.registry_path)
        raw = data.raw if isinstance(data.raw, dict) else {}
        plugins = raw.get("plugins")
        if not isinstance(plugins, list):
            plugins = []
        kept = [e for e in plugins
                if not (isinstance(e, dict) and
                        plugin_registry.entry_owner(e) == f"specialist:{self.slug}")]
        kept.extend(self.before_entries)
        raw["plugins"] = kept
        data.raw = raw
        plugin_registry.save_registry(data, self.registry_path)

        # 2. Tuple/sidecar files: write recorded content back; delete files
        # recorded as absent (content is None).
        slug_dir = Path(self.specialists_dir) / self.slug
        for filename, content in self.before_tuple_files.items():
            target = slug_dir / filename
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        # 3. Consent-ack records: slug-scoped delta re-insert (never a
        # whole-map rewrite — see SpecialistInstallAckStore.restore_records).
        SpecialistInstallAckStore(self.acks_path).restore_records(
            self.ack_records)


def quarantine(slug: str, *,
                registry_path: Path = plugin_registry.REGISTRY_PATH) -> None:
    """Remove every registry entry owned by `slug` and flag the slug in the
    registry raw doc's `quarantined_bundles` list (surfaced by health,
    Task 11)."""
    registry_path = Path(registry_path)
    data = plugin_registry.load_registry(registry_path)
    raw = data.raw if isinstance(data.raw, dict) else {}
    plugins = raw.get("plugins")
    if isinstance(plugins, list):
        raw["plugins"] = [
            e for e in plugins
            if not (isinstance(e, dict) and
                    plugin_registry.entry_owner(e) == f"specialist:{slug}")
        ]
    qlist = raw.setdefault("quarantined_bundles", [])
    if slug not in qlist:
        qlist.append(slug)
    data.raw = raw
    plugin_registry.save_registry(data, registry_path)


def quarantine_all(*,
                    registry_path: Path = plugin_registry.REGISTRY_PATH) -> None:
    """Deterministic worst case: an unparseable journal filename carries no
    trustworthy slug, so every owner-bearing entry is removed and every
    owning slug flagged."""
    registry_path = Path(registry_path)
    data = plugin_registry.load_registry(registry_path)
    raw = data.raw if isinstance(data.raw, dict) else {}
    plugins = raw.get("plugins")
    slugs: set[str] = set()
    if isinstance(plugins, list):
        kept = []
        for e in plugins:
            owner = plugin_registry.entry_owner(e) if isinstance(e, dict) else None
            if owner is not None:
                slugs.add(owner.split(":", 1)[1])
                continue
            kept.append(e)
        raw["plugins"] = kept
    qlist = raw.setdefault("quarantined_bundles", [])
    for s in sorted(slugs):
        if s not in qlist:
            qlist.append(s)
    data.raw = raw
    plugin_registry.save_registry(data, registry_path)


def _valid_payload(payload: Any, slug: str) -> bool:
    """Strict, jsonschema-shaped structural validation (spec §3.1): schema
    shape, `payload["slug"] == filename slug`, and tuple-path containment —
    every `before.tuple_files` key must be one of the fixed six filenames.
    Never raises — any unexpected shape is simply invalid."""
    try:
        if not isinstance(payload, dict):
            return False
        if payload.get("schema_version") != SCHEMA_VERSION:
            return False
        if not isinstance(payload.get("op"), str) or not payload["op"]:
            return False
        if payload.get("slug") != slug:
            return False
        if payload.get("state") not in ("in-progress", "complete"):
            return False
        before = payload.get("before")
        if not isinstance(before, dict):
            return False
        entries = before.get("registry_entries")
        if not isinstance(entries, list) or not all(
            isinstance(e, dict) for e in entries
        ):
            return False
        tuple_files = before.get("tuple_files")
        if not isinstance(tuple_files, dict):
            return False
        for key, value in tuple_files.items():
            if key not in TUPLE_FILENAMES:
                return False
            if value is not None and not isinstance(value, str):
                return False
        ack_records = before.get("ack_records")
        if not isinstance(ack_records, list) or not all(
            isinstance(r, dict) for r in ack_records
        ):
            return False
        return True
    except Exception:  # noqa: BLE001 — strict validation must never raise
        return False


def _quarantine_rename(path: Path) -> None:
    os.replace(path, path.with_name(path.name + ".quarantined"))


def reconcile_boot(*, ops_dir: Path = OPS_DIR,
                    registry_path: Path = plugin_registry.REGISTRY_PATH,
                    specialists_dir: Path = SPECIALISTS_DIR,
                    acks_path: Path = ACKS_PATH) -> list[dict]:
    """Scan EVERY regular file in `ops_dir` (skipping `*.quarantined`) and
    reconcile it per the module docstring. Runs before the plugin snapshot
    loads. Returns `[{slug, action}]` for the health report; also stashed on
    `last_boot_reconcile_actions`. Idempotent — safe to run twice."""
    global last_boot_reconcile_actions
    ops_dir = Path(ops_dir)
    registry_path = Path(registry_path)
    specialists_dir = Path(specialists_dir)
    acks_path = Path(acks_path)
    actions: list[dict] = []

    if not ops_dir.is_dir():
        last_boot_reconcile_actions = actions
        return actions

    for path in sorted(ops_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name.endswith(".quarantined"):
            continue

        match = JOURNAL_NAME_RE.match(path.name)
        if match is None:
            try:
                quarantine_all(registry_path=registry_path)
            except Exception:  # noqa: BLE001 — degrade-and-boot
                logger.exception(
                    "quarantine_all failed for unparseable journal %s", path)
            _quarantine_rename(path)
            actions.append({"slug": None, "action": "quarantine_all"})
            continue

        slug = match.group("slug")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None

        if not _valid_payload(payload, slug):
            try:
                quarantine(slug, registry_path=registry_path)
            except Exception:  # noqa: BLE001 — degrade-and-boot
                logger.exception("quarantine failed for slug %s", slug)
            _quarantine_rename(path)
            actions.append({"slug": slug, "action": "quarantine"})
            continue

        if payload["state"] == "complete":
            # Crash between the complete-write and the unlink: the op
            # already finished — prune WITHOUT rollback.
            path.unlink()
            actions.append({"slug": slug, "action": "pruned_complete"})
            continue

        before = payload["before"]
        txn = BundleTxn(
            journal_path=path,
            slug=slug,
            before_entries=before["registry_entries"],
            before_tuple_files=before["tuple_files"],
            ack_records=before["ack_records"],
            registry_path=registry_path,
            specialists_dir=specialists_dir,
            acks_path=acks_path,
        )
        try:
            txn.rollback_disk()
        except Exception:  # noqa: BLE001 — degrade-and-boot
            logger.exception("rollback failed for slug %s; quarantining", slug)
            try:
                quarantine(slug, registry_path=registry_path)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "quarantine failed for slug %s after rollback failure", slug)
            _quarantine_rename(path)
            actions.append({"slug": slug, "action": "quarantine"})
            continue

        path.unlink()
        actions.append({"slug": slug, "action": "rolled_back"})

    last_boot_reconcile_actions = actions
    return actions
