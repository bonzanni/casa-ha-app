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
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger("config_sync")

# In-scope trees, relative to each of the three roots. schema/ keeps its
# own always-overwrite handling in setup-configs.sh and is out of scope here.
SYNC_TREES = ("agents", "policies", "bindings", "specialists")


@dataclass
class SyncReport:
    image_version: str
    pre_sync_sha: str | None = None
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    schema_forced: list[dict] = field(default_factory=list)
    casabak: list[str] = field(default_factory=list)
    # Finding 2 (theme 8/9): the POST-SYNC boot-parity validation of the
    # reconciled /config tree. ``post_sync_errors`` are boot-fatal
    # inconsistencies the reconciler could NOT self-heal (surfaced loudly);
    # ``post_sync_healed`` are files the reconciler removed to keep boot alive.
    post_sync_errors: list[str] = field(default_factory=list)
    post_sync_healed: list[str] = field(default_factory=list)
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


# Matches agent_loader's delegates-without-delegate-tool boot fatal, e.g.
#   agent 'assistant': delegates.yaml is non-empty but runtime.yaml ...
_DELEGATE_MISMATCH_RE = re.compile(
    r"agent '([^']+)': delegates\.yaml is non-empty but")


def _post_sync_validate_and_heal(
    *, config_dir: Path, defaults_dir: Path, report: SyncReport,
    validate_repo: Callable[[], list[str]] | None,
) -> None:
    """Finding 2 backstop: validate the POST-SYNC /config tree with the boot
    loader and repair-or-surface any boot-fatal inconsistency the reconciler
    itself introduced by re-injecting an image-owned default.

    The known case: the committed tree validly dropped an image-owned
    ``agents/<role>/delegates.yaml`` (and the delegate tool from
    runtime.yaml), which passes the pre-commit gate — but config_sync
    re-seeds the image-owned delegates.yaml here, producing a
    delegates-without-delegate-tool mismatch that FATALs the next boot.

    Best-effort and boot-safe: any error inside this backstop is swallowed so
    the backstop can never itself block boot.
    """
    if validate_repo is None:
        return
    try:
        errors = validate_repo()
    except Exception as exc:  # noqa: BLE001 — backstop must never crash boot
        logger.warning("config_sync post-sync validation raised (ignored): %s", exc)
        return
    if not errors:
        return

    # Self-heal the delegates/tool case: drop the re-injected image-owned
    # delegates.yaml so boot survives. Only when the on-disk file is byte-equal
    # to the image default (proof it is the image-owned copy, not an operator's
    # genuine delegates content) — never delete real user delegation config.
    healed = False
    for err in errors:
        m = _DELEGATE_MISMATCH_RE.search(err)
        if not m:
            continue
        role = m.group(1)
        rel = f"agents/{role}/delegates.yaml"
        live = config_dir / rel
        default = defaults_dir / rel
        if live.exists() and default.exists() and _bytes_equal(live, default):
            _delete(config_dir, rel)
            report.post_sync_healed.append(rel)
            healed = True
            logger.warning(
                "config_sync: removed re-injected image-owned %s to keep boot "
                "alive — %s. Reconcile runtime.yaml tools.allowed (add the "
                "delegate tool) or the image delegates default to make this "
                "durable.", rel, err,
            )

    # Re-validate after any heal so the report reflects the true residual state.
    if healed:
        try:
            errors = validate_repo()
        except Exception as exc:  # noqa: BLE001
            logger.warning("config_sync post-heal validation raised (ignored): %s", exc)
            errors = []

    report.post_sync_errors = list(errors)
    for err in errors:
        logger.error(
            "config_sync POST-SYNC boot-parity error (next boot will FATAL "
            "unless fixed): %s", err,
        )


def reconcile(*, defaults_dir, config_dir, baseline_dir,
              image_version: str, git, validate: Callable[[str], str | None],
              validate_repo: Callable[[], list[str]] | None = None) -> SyncReport:
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
                # No `or git.head()` fallback: git.snapshot() now returns None
                # ONLY when the snapshot actually failed (e.g. dubious-ownership
                # or a stale index.lock). Falling back to a stale pre-edit HEAD
                # would record a misleading recovery pointer for an edit the
                # commit never captured — treat a failed snapshot as degraded
                # (sha None) so the caller writes a .casabak instead (M12).
                pre_sync.append(git.snapshot(
                    "casa-sync: pre-sync snapshot before default reconcile"))
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
        if sha is None:
            # No commit captured the user's edit (git unavailable OR present
            # but failing) — snapshot it to a .casabak before clobbering so the
            # edit is always recoverable; never silently destroy it (M12).
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
        if sha is None:
            # See conflict-site note: snapshot to .casabak whenever no commit
            # captured the edit (git unavailable or failing) before clobbering.
            _archive_casabak(config_dir, rel, report)
        _copy(defaults_dir, rel, config_dir)
        report.schema_forced.append({"path": rel, "pre_sync_sha": sha})

    _mirror_baseline(defaults_dir, baseline_dir)

    # Finding 2 backstop — validate the reconciled tree with the real boot
    # loader and repair-or-surface any inconsistency the re-seed introduced.
    _post_sync_validate_and_heal(
        config_dir=config_dir, defaults_dir=defaults_dir, report=report,
        validate_repo=validate_repo,
    )

    changed = bool(
        report.updated or report.deleted or report.conflicts
        or report.schema_forced or report.post_sync_healed
    )
    if changed and git.available:
        git.snapshot(f"casa-sync: default reconcile {image_version}")

    return report


def _mirror_baseline(defaults_dir: Path, baseline_dir: Path) -> None:
    """Replace baseline SYNC_TREES with an exact copy of the new defaults."""
    for tree in SYNC_TREES:
        dst = Path(baseline_dir) / tree
        src = Path(defaults_dir) / tree
        if dst.exists():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst)


class RealGit:
    """Git shim over the /config repo. Commits all pending changes and
    returns the resulting HEAD sha. ``available`` is False when git is
    missing or *repo* is not a git work-tree (degraded → .casabak path)."""

    def __init__(self, repo) -> None:
        self.repo = str(repo)
        self.available = bool(shutil.which("git")) and Path(self.repo, ".git").is_dir()

    def _run(self, *args: str):
        return __import__("subprocess").run(
            ["git", "-C", self.repo, *args],
            capture_output=True, text=True,
        )

    def head(self) -> str | None:
        if not self.available:
            return None
        res = self._run("rev-parse", "HEAD")
        return res.stdout.strip() if res.returncode == 0 else None

    def snapshot(self, message: str) -> str | None:
        """Commit all pending /config changes; return the resulting HEAD.

        Fails CLOSED: returns None on ANY git error (dubious-ownership, a
        stale index.lock left by a crash mid-commit, a corrupt repo) so the
        reconciler treats the snapshot as not-taken and falls back to a
        .casabak instead of clobbering a user edit uncaptured (M12).
        """
        if not self.available:
            return None
        add = self._run("add", "-A")
        if add.returncode != 0:
            logger.warning("config_sync: git add failed: %s", add.stderr.strip())
            return None
        # `git diff --cached --quiet` exit codes: 0=clean, 1=staged changes,
        # >=2 (e.g. 128)=git error. The old code conflated error with "clean"
        # and skipped the commit, then returned a stale pre-edit HEAD.
        staged = self._run("diff", "--cached", "--quiet")
        if staged.returncode == 1:
            commit = self._run(
                "-c", "user.email=casa-agent@local",
                "-c", "user.name=Casa Agent",
                "commit", "-q", "-m", message,
            )
            if commit.returncode != 0:
                logger.warning(
                    "config_sync: git commit failed: %s", commit.stderr.strip())
                return None
        elif staged.returncode != 0:
            logger.warning(
                "config_sync: git diff --cached failed: %s", staged.stderr.strip())
            return None
        return self.head()


def _make_validator(config_dir) -> Callable[[str], str | None]:
    """Validator backed by agent_loader's schema maps + _validate, checking
    a live file at *config_dir/rel* against the NEW image schema (agent_loader
    reads defaults/schema, which is this image's schema). Returns an error
    string when invalid (incl. YAML parse errors), else None. Files with no
    associated schema return None."""
    import agent_loader as al

    config_dir = Path(config_dir)

    def validate(rel: str) -> str | None:
        name = Path(rel).name
        parts = Path(rel).parts
        abs_path = str(config_dir / rel)
        try:
            if parts and parts[0] == "agents":
                schema_name = al._SCHEMA_BY_FILENAME.get(name)
                if schema_name is None:
                    return None
                al._validate(al._read_yaml(abs_path), schema_name, abs_path)
            elif parts and parts[0] == "policies":
                mapping = al._SCHEMA_BY_POLICY_FILE.get(name)
                if mapping is None:
                    return None
                schema_name, version = mapping
                al._validate(al._read_yaml(abs_path), schema_name, abs_path, version=version)
            else:
                return None
        except al.LoadError as exc:
            return str(exc)
        return None

    return validate


def _make_repo_validator(config_dir) -> Callable[[], list[str]]:
    """Post-sync whole-tree validator backed by agent_loader's boot-parity
    ``validate_config_repo`` (hardened in v0.55.0 to faithfully catch boot
    fatals). Returns the list of error strings for the reconciled /config
    tree; used by the Finding 2 backstop to repair-or-surface inconsistencies
    the re-seed introduces."""
    import agent_loader as al

    def validate_repo() -> list[str]:
        return al.validate_config_repo(str(config_dir))

    return validate_repo


def run(*, defaults_dir, config_dir, baseline_dir, report_path,
        image_version: str) -> int:
    """Boot/reload entry point. Non-fatal by contract: logs and returns 0
    on any unexpected error so a reconciler bug never blocks boot."""
    try:
        git = RealGit(config_dir)
        validate = _make_validator(config_dir)
        validate_repo = _make_repo_validator(config_dir)
        report = reconcile(
            defaults_dir=defaults_dir, config_dir=config_dir,
            baseline_dir=baseline_dir, image_version=image_version,
            git=git, validate=validate, validate_repo=validate_repo,
        )
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(report.to_json(), encoding="utf-8")
        logger.info(
            "config_sync: updated=%d deleted=%d conflicts=%d schema_forced=%d "
            "casabak=%d post_sync_healed=%d post_sync_errors=%d",
            len(report.updated), len(report.deleted), len(report.conflicts),
            len(report.schema_forced), len(report.casabak),
            len(report.post_sync_healed), len(report.post_sync_errors),
        )
    except Exception as exc:  # noqa: BLE001 — boot-critical: never fatal
        logger.warning("config_sync: reconcile failed (non-fatal): %s", exc)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] config_sync: %(message)s")
    config_dir = os.environ.get("CASA_CONFIG_DIR", "/config")
    defaults_dir = os.environ.get("CASA_DEFAULTS_DIR", "/opt/casa/defaults")
    data_dir = os.environ.get("CASA_DATA_DIR", "/data")
    image_version = os.environ.get("CASA_IMAGE_VERSION", "unknown")
    return run(
        defaults_dir=defaults_dir,
        config_dir=config_dir,
        baseline_dir=os.path.join(data_dir, "config-baseline"),
        report_path=os.path.join(data_dir, "config-sync-report.json"),
        image_version=image_version,
    )


if __name__ == "__main__":
    raise SystemExit(main())
