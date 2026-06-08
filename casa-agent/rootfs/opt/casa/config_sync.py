"""Three-way /config default-sync reconciler.

Makes image-default-owned config under /config/{agents,policies} track the
shipped defaults at /opt/casa/defaults, preserving genuine runtime edits.
Image-wins on true conflict, made safe by a commit-first snapshot to
/config/.git; a schema backstop forces image-wins on any kept-live file
invalid against the new schema so casa always boots.

Spec: docs/superpowers/specs/2026-06-08-config-sync-reconciler-design.md.
Pure-Python and dependency-injected (git + validator) for unit testing;
__main__ supplies real implementations.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger("config_sync")

# In-scope trees, relative to each of the three roots. schema/ keeps its
# own always-overwrite handling in setup-configs.sh and is out of scope here.
SYNC_TREES = ("agents", "policies")


@dataclass
class SyncReport:
    image_version: str
    pre_sync_sha: str | None = None
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    schema_forced: list[dict] = field(default_factory=list)
    casabak: list[str] = field(default_factory=list)
    notified: bool = False

    def has_overwrites(self) -> bool:
        return bool(self.conflicts or self.schema_forced or self.casabak)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _list_tree_files(root: Path) -> set[str]:
    """Relative posix paths of regular files under SYNC_TREES of *root*.

    Skips any `.git/` path and `.casabak` sidecars.
    """
    out: set[str] = set()
    root = Path(root)
    for tree in SYNC_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel.startswith(".git/") or "/.git/" in f"/{rel}":
                continue
            if rel.endswith(".casabak"):
                continue
            out.add(rel)
    return out


def _bytes_equal(a: Path, b: Path) -> bool:
    try:
        return Path(a).read_bytes() == Path(b).read_bytes()
    except OSError:
        return False


def _copy(src_root: Path, rel: str, dst_root: Path) -> None:
    dst = Path(dst_root) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(src_root) / rel, dst)


def _delete(root: Path, rel: str) -> None:
    p = Path(root) / rel
    try:
        p.unlink()
    except FileNotFoundError:
        return
    # Prune now-empty parent dirs up to (not including) the tree root.
    parent = p.parent
    root = Path(root)
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _archive_casabak(config_dir: Path, rel: str, report: SyncReport) -> None:
    src = Path(config_dir) / rel
    bak = src.with_name(src.name + ".casabak")
    shutil.copy2(src, bak)
    report.casabak.append(rel)


def reconcile(*, defaults_dir, config_dir, baseline_dir,
              image_version: str, git, validate: Callable[[str], str | None]) -> SyncReport:
    defaults_dir = Path(defaults_dir)
    config_dir = Path(config_dir)
    baseline_dir = Path(baseline_dir)
    report = SyncReport(image_version=image_version)

    new_files = _list_tree_files(defaults_dir)
    base_files = _list_tree_files(baseline_dir)
    live_files = _list_tree_files(config_dir)

    # Lazy pre-sync snapshot — taken once, before the first image-wins overwrite.
    pre_sync: list[str | None] = []  # box: empty = not captured yet

    def _ensure_pre_sync() -> str | None:
        if not pre_sync:
            if git.available:
                sha = git.snapshot("casa-sync: pre-sync snapshot before default reconcile")
                pre_sync.append(sha or git.head())
            else:
                pre_sync.append(None)
            report.pre_sync_sha = pre_sync[0]
        return pre_sync[0]

    for rel in sorted(new_files | base_files | live_files):
        new_ex = rel in new_files
        base_ex = rel in base_files
        live_ex = rel in live_files

        if not live_ex:
            if new_ex:
                _copy(defaults_dir, rel, config_dir)      # create / seed
                report.updated.append(rel)
            continue                                       # baseline-only & gone: baseline rewrite drops it

        if not base_ex:
            continue                                       # adopt: no ownership proof → keep live

        live_eq_base = _bytes_equal(config_dir / rel, baseline_dir / rel)
        if live_eq_base:                                   # untouched
            if not new_ex:
                _delete(config_dir, rel)
                report.deleted.append(rel)
            elif not _bytes_equal(defaults_dir / rel, baseline_dir / rel):
                _copy(defaults_dir, rel, config_dir)       # image changed → track
                report.updated.append(rel)
            continue

        # live edited
        if not new_ex:
            continue                                       # edited + removed-from-defaults → keep live
        if _bytes_equal(defaults_dir / rel, baseline_dir / rel):
            continue                                       # image unchanged → keep live
        if _bytes_equal(config_dir / rel, defaults_dir / rel):
            continue                                       # converged
        # conflict → image wins
        sha = _ensure_pre_sync()
        if not git.available:
            _archive_casabak(config_dir, rel, report)
        _copy(defaults_dir, rel, config_dir)
        report.conflicts.append({"path": rel, "pre_sync_sha": sha})

    # --- Schema backstop (spec §3.4): any kept-live file invalid against the
    # new schema is force-overwritten with the default so boot can't FATAL.
    for rel in sorted(_list_tree_files(config_dir)):
        if rel not in new_files:
            continue                                   # no default to fall back to
        if _bytes_equal(config_dir / rel, defaults_dir / rel):
            continue                                   # already the default → valid by construction
        err = validate(rel)
        if not err:
            continue
        logger.warning("config_sync backstop: %s invalid vs new schema (%s) — forcing default", rel, err)
        sha = _ensure_pre_sync()
        if not git.available:
            _archive_casabak(config_dir, rel, report)
        _copy(defaults_dir, rel, config_dir)
        report.schema_forced.append({"path": rel, "pre_sync_sha": sha})

    return report
