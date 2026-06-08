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
