"""Unified plugin architecture — registry + resolver (spec §3.1-§3.3).

The registry file /config/plugins/registry.json is the SINGLE plugin-
assignment authority for every agent tier. Entries pin an immutable
content-addressed artifact in /config/plugins/store/<name>/<artifact-id>/.

Failure scoping: unparseable JSON / unsupported schema_version = registry-
wide invalid; a malformed individual entry = per-entry skip with a recorded
issue. One bad entry never defeats per-plugin degradation.

STDLIB-ONLY imports at module level (the Dockerfile build helper imports
this before the venv exists); atomic_io / plugin_store are imported lazily.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path("/config/plugins/registry.json")
DEFAULT_REGISTRY_PATH = Path("/opt/casa/defaults/plugin-registry.json")
STORE_ROOT = Path("/config/plugins/store")

SCHEMA_VERSION = 1

TARGET_RE = re.compile(r"^(resident|specialist|executor):[a-z0-9][a-z0-9_-]*$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REVISION_RE = re.compile(r"^(git:[0-9a-f]{40}|legacy-content:[0-9a-f]{64})$")
_SOURCE_TYPES = {"github", "bundled"}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_repo(repo: str) -> str:
    return repo.strip().lower()


def normalize_subdir(subdir: str) -> str:
    """Canonical POSIX-relative form (spec 3.2). Collapses repeated
    separators, strips edge slashes; REJECTS backslashes and any '.'/'..'
    segment (identity must never be traversal-ambiguous)."""
    s = subdir.strip()
    if "\\" in s:
        raise ValueError(f"backslash in subdir: {subdir!r}")
    parts = [seg for seg in s.split("/") if seg != ""]
    if any(seg in (".", "..") for seg in parts):
        raise ValueError(f"non-canonical subdir: {subdir!r}")
    return "/".join(parts)


def compute_artifact_id(*, repo: str, revision: str, subdir: str,
                        name: str) -> str:
    ident = "\n".join(
        [normalize_repo(repo), revision, normalize_subdir(subdir), name],
    ).encode("utf-8")
    return hashlib.sha256(ident).hexdigest()


@dataclass(frozen=True)
class PluginIssue:
    name: str
    target: str | None
    stage: str
    reason_code: str
    artifact_id: str | None = None
    # Sol R2-2: for registry-stage issues, the RAW entry's OWN parseable
    # targets, captured at validation time — issue attribution is by entry,
    # never reconstructed by name (a same-named valid entry must not lend
    # its targets to an unscopable invalid one). Not part of the health
    # fingerprint (spec 3.10 uses the first five fields only).
    scoped_targets: tuple[str, ...] = ()


@dataclass
class RegistryData:
    raw: dict
    entries: list[dict] = field(default_factory=list)
    entry_issues: list[PluginIssue] = field(default_factory=list)
    valid: bool = True


def _entry_error(entry: object) -> str | None:
    """Return a reason string if the entry is malformed, else None."""
    if not isinstance(entry, dict):
        return "not_a_mapping"
    name = entry.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "bad_name"
    src = entry.get("source")
    if not isinstance(src, dict):
        return "bad_source"
    if src.get("type") not in _SOURCE_TYPES:
        return "bad_source_type"
    repo = src.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        return "bad_repo"
    if not isinstance(src.get("ref"), str) or not src["ref"]:
        return "bad_ref"
    revision = src.get("revision")
    if not isinstance(revision, str) or not REVISION_RE.match(revision):
        return "bad_revision"
    subdir = src.get("subdir", "")
    if not isinstance(subdir, str):
        return "bad_subdir"
    try:
        normalize_subdir(subdir)
    except ValueError:
        return "bad_subdir"
    if not isinstance(entry.get("version"), str) or not entry["version"]:
        return "bad_version"
    targets = entry.get("targets")
    if not isinstance(targets, list) or not all(
        isinstance(t, str) and TARGET_RE.match(t) for t in targets
    ):
        return "bad_targets"
    expected = compute_artifact_id(
        repo=repo, revision=revision, subdir=subdir, name=name,
    )
    if entry.get("artifact_id") != expected:
        return "artifact_id_mismatch"
    return None


def _own_targets(raw_entry: object) -> tuple[str, ...]:
    """Parseable targets of THIS raw entry only (Sol R2-2)."""
    if not isinstance(raw_entry, dict):
        return ()
    targets = raw_entry.get("targets")
    if not isinstance(targets, list):
        return ()
    return tuple(t for t in targets
                 if isinstance(t, str) and TARGET_RE.match(t))


def _validate_doc(raw: object) -> tuple[list[dict], list[PluginIssue], bool]:
    """Validate a parsed registry document (spec §3.1). Returns
    (entries, issues, valid). registry-wide invalid ⇒ ([], [], False).
    Single validator shared by load_registry and _revalidate (DRY)."""
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        logger.warning("plugin registry unsupported schema_version")
        return [], [], False
    seeded = raw.setdefault("seeded_defaults", [])
    if (not isinstance(seeded, list)
            or not all(isinstance(x, str) and NAME_RE.match(x) for x in seeded)
            or len(set(seeded)) != len(seeded)):   # NAME_RE + uniqueness
        logger.warning("plugin registry seeded_defaults malformed")
        return [], [], False
    plugins = raw.get("plugins")
    if not isinstance(plugins, list):
        return [], [], False

    entries: list[dict] = []
    issues: list[PluginIssue] = []
    for entry in plugins:
        if _entry_error(entry) is not None:
            nm = entry.get("name") if isinstance(entry, dict) else "?"
            issues.append(PluginIssue(
                name=str(nm), target=None, stage="registry",
                reason_code="entry_invalid",
                artifact_id=(entry.get("artifact_id")
                             if isinstance(entry, dict) else None),
                scoped_targets=_own_targets(entry),
            ))
            continue
        entries.append(entry)

    # Uniqueness: name is the PK — collisions skip BOTH entries (§3.1).
    by_name: dict[str, list[dict]] = {}
    for e in entries:
        by_name.setdefault(e["name"], []).append(e)
    kept_ids: set[int] = set()
    for name, group in by_name.items():
        if len(group) > 1:
            for e in group:
                issues.append(PluginIssue(
                    name=name, target=None, stage="registry",
                    reason_code="duplicate_name",
                    artifact_id=e.get("artifact_id"),
                    scoped_targets=_own_targets(e),   # each entry's OWN
                ))
        else:
            kept_ids.add(id(group[0]))
    # Preserve original file order for kept entries.
    ordered = [e for e in entries if id(e) in kept_ids]
    return ordered, issues, True


def load_registry(path: Path = REGISTRY_PATH) -> RegistryData:
    if not Path(path).is_file():
        return RegistryData(raw={"schema_version": SCHEMA_VERSION,
                                 "seeded_defaults": [], "plugins": []})
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("plugin registry unreadable (%s): %s", path, exc)
        return RegistryData(raw={}, valid=False)
    entries, issues, valid = _validate_doc(raw)
    return RegistryData(raw=raw if isinstance(raw, dict) else {},
                        entries=entries, entry_issues=issues, valid=valid)


def save_registry(data: RegistryData, path: Path = REGISTRY_PATH) -> None:
    """Atomic write. `data.raw` is the document of record (unknown fields
    preserved); `data.entries` view into raw['plugins'] — mutations to
    entries must be applied to raw['plugins'] by the caller before save."""
    from atomic_io import atomic_write_text  # lazy: not needed at build time
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(Path(path),
                      json.dumps(data.raw, indent=2, sort_keys=False) + "\n")


def _revalidate(data: RegistryData) -> None:
    """Refresh data.entries/entry_issues/valid to reflect data.raw. Entries
    returned are live views into data.raw['plugins']."""
    entries, issues, valid = _validate_doc(data.raw)
    data.entries, data.entry_issues, data.valid = entries, issues, valid


def seed_defaults(data: RegistryData,
                  default_path: Path = DEFAULT_REGISTRY_PATH) -> bool:
    """§3.1 default seeding without resurrection. A default is added ONLY if
    its name is absent from BOTH plugins and seeded_defaults; every seeded
    name is appended to seeded_defaults permanently. Returns True if mutated."""
    try:
        defaults = json.loads(Path(default_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    present = {e.get("name") for e in data.raw.get("plugins", [])
               if isinstance(e, dict)}
    seeded = data.raw.setdefault("seeded_defaults", [])
    mutated = False
    for entry in defaults.get("plugins", []):
        name = entry.get("name")
        if not name or name in present or name in seeded:
            continue
        data.raw.setdefault("plugins", []).append(dict(entry))
        seeded.append(name)
        mutated = True
    if mutated:
        _revalidate(data)
    return mutated


@dataclass(frozen=True)
class ResolvedPlugin:
    name: str
    artifact_id: str
    path: str
    version: str
    manifest: dict


@dataclass
class ResolutionResult:
    registry_valid: bool
    plugins: list[ResolvedPlugin] = field(default_factory=list)
    issues: list[PluginIssue] = field(default_factory=list)
    warnings: list[PluginIssue] = field(default_factory=list)
    # D2 (v0.74.0): the snapshot generation this resolution was computed
    # against — consumers (Agent binding snapshots, the mutation's post-reload
    # check) detect an intervening reload instead of grading stale state.
    generation: int = 0


@dataclass(frozen=True)
class _Snapshot:
    registry: RegistryData
    registry_path: Path
    store_root: Path
    # D2 (v0.74.0): the monotonic generation is a FIELD of the frozen
    # snapshot — published in the same single `_snapshot = …` assignment, so
    # no reader can observe a new snapshot with an old generation (the old
    # separate `_generation` global was itself a torn pair).
    generation: int = 0
    # (name, artifact_id) -> reason_code | None (None == deep-valid).
    validation: dict[tuple[str, str], str | None] = field(default_factory=dict)


_snapshot: _Snapshot | None = None
# r2-B2 (v0.74.0): reload_snapshot runs on worker threads (the mutation tools
# hold the asyncio _PLUGIN_TOOLS_LOCK, but the manual-edit seam reload_full
# does NOT serialize with them at this layer) — the generation
# read-modify-write must be a critical section or two concurrent reloads can
# publish the same generation, defeating the intervening-reload fence.
# threading.Lock (not asyncio) because every caller is in a to_thread context.
_RELOAD_LOCK = threading.Lock()


def snapshot_generation() -> int:
    """The current snapshot's monotonic generation (bumped by every
    reload_snapshot). Reads the one published snapshot object."""
    return _current().generation


def reload_snapshot(*, registry_path: Path = REGISTRY_PATH,
                    store_root: Path = STORE_ROOT) -> None:
    """§3.9 mutation-sequencing seam: refresh the in-memory snapshot from
    disk BEFORE agents are reconstructed. reload_full and every registry-
    mutating tool call this; nothing else re-reads the registry file.

    Sol F1: deep validation (full checksum, plugin_store.artifact_verdict)
    runs HERE, once per referenced artifact per snapshot, cached — resolve_for
    never checksums.

    r2-B2 (v0.74.0): the ENTIRE load/deep-validate/publish sequence is
    serialized under _RELOAD_LOCK so concurrent reloads can never publish
    duplicate generations; the snapshot itself is published by one
    assignment of a frozen object."""
    import plugin_store  # local import: plugin_store imports this module
    global _snapshot
    with _RELOAD_LOCK:
        reg = load_registry(registry_path)
        validation: dict[tuple[str, str], str | None] = {}
        if reg.valid:
            for entry in reg.entries:
                key = (entry["name"], entry["artifact_id"])
                path = Path(store_root) / entry["name"] / entry["artifact_id"]
                if not path.is_dir():
                    validation[key] = "artifact_missing"
                else:
                    # Sol R2-1: the ONE deep validator — identity vs the ENTRY
                    # (name/repo/revision/subdir/artifact_id) + checksum.
                    validation[key] = plugin_store.artifact_verdict(
                        path,
                        name=entry["name"],
                        repo=entry["source"]["repo"],
                        revision=entry["source"]["revision"],
                        subdir=entry["source"].get("subdir", ""),
                        artifact_id=entry["artifact_id"],
                    )
        _snapshot = _Snapshot(
            registry=reg, registry_path=Path(registry_path),
            store_root=Path(store_root),
            generation=(_snapshot.generation + 1
                        if _snapshot is not None else 1),
            validation=validation)


def _current() -> _Snapshot:
    if _snapshot is None:
        reload_snapshot()
    return _snapshot


def snapshot_registry() -> RegistryData:
    return _current().registry


def _resolve_entry(entry: dict, snap: "_Snapshot", target: str | None,
                   ) -> tuple[ResolvedPlugin | None, PluginIssue | None,
                              PluginIssue | None]:
    """Returns (plugin, issue, warning) — plugin XOR issue."""
    name = entry["name"]
    artifact_id = entry["artifact_id"]
    store_root = snap.store_root
    deep = snap.validation.get((name, artifact_id), "artifact_missing")
    if deep is not None:   # cached artifact_verdict result (Sol F1/R2-1):
        return None, PluginIssue(name=name, target=target, stage="resolve",
                                 reason_code=deep,
                                 artifact_id=artifact_id), None
    path = Path(store_root) / name / artifact_id
    try:
        manifest = json.loads(
            (path / ".claude-plugin" / "plugin.json")
            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Raced-away since snapshot load — treat as invalid, never partial.
        return None, PluginIssue(name=name, target=target, stage="resolve",
                                 reason_code="artifact_invalid",
                                 artifact_id=artifact_id), None
    warning = None
    if entry["source"]["revision"].startswith("legacy-content:"):
        warning = PluginIssue(name=name, target=target, stage="resolve",
                              reason_code="legacy_provenance",
                              artifact_id=artifact_id)
    return ResolvedPlugin(
        name=name, artifact_id=artifact_id, path=str(path),
        version=str(manifest.get("version", entry.get("version", ""))),
        manifest=manifest,
    ), None, warning


def _resolve(target: str | None) -> ResolutionResult:
    snap = _current()
    reg = snap.registry
    if not reg.valid:
        return ResolutionResult(registry_valid=False,
                                generation=snap.generation)
    result = ResolutionResult(registry_valid=True, generation=snap.generation)
    for issue in reg.entry_issues:   # stage="registry"; Sol F2 + R2-2:
        # attribution rides on the issue's OWN scoped_targets, captured at
        # validation time from its own raw entry — never name-matched.
        if target is None:
            result.issues.append(issue)
        elif target in issue.scoped_targets:
            result.issues.append(PluginIssue(
                name=issue.name, target=target, stage=issue.stage,
                reason_code=issue.reason_code, artifact_id=issue.artifact_id,
                scoped_targets=issue.scoped_targets))
    for entry in reg.entries:
        if target is not None and target not in entry.get("targets", []):
            continue
        plugin, issue, warning = _resolve_entry(entry, snap, target)
        if plugin is not None:
            result.plugins.append(plugin)
        if issue is not None:
            result.issues.append(issue)
        if warning is not None:
            result.warnings.append(warning)
    return result


def resolve_for(target: str) -> ResolutionResult:
    return _resolve(target)


def resolve_all() -> ResolutionResult:
    return _resolve(None)
