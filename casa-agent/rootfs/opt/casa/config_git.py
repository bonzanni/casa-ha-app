"""Git-backed history for ``/config/``.

Local-only repo: no remote, no push. The builder agent in Spec Y uses
``commit_config`` on every write and ``restore_file`` to roll back.
Casa boot uses ``init_repo`` (idempotent) and ``snapshot_manual_edits``
(records uncommitted human edits before the builder can trip over them).

Wraps the ``git`` CLI via :mod:`subprocess` — keeps the dependency
footprint zero (no libgit2, no dulwich).
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Sequence

logger = logging.getLogger(__name__)


_GITIGNORE_CONTENT = """\
# Casa config repo — track configs only.
*
!agents/
!agents/**
!policies/
!policies/**
!bindings/
!bindings/**
!schema/
!schema/**
# Unified plugin architecture (v0.71.0): the registry is config — the single
# plugin-assignment authority — and versioning it gives an audit trail.
# ONLY registry.json: the artifact store and staging under plugins/ are
# content-addressed binaries, never tracked.
!plugins/
!plugins/registry.json
plugins/store/
plugins/.staging/
# Installed-specialist data model (Task 13): registry.json is config — same
# audit-trail rationale as plugins/registry.json above. ONLY the per-slug
# active/desired/prior tuples and the top-level registry are tracked; the
# content-addressed component store and staging are binaries, never tracked.
!specialists/
!specialists/registry.json
!specialists/*/active.yaml
!specialists/*/desired.yaml
!specialists/*/active.prior.yaml
specialists/store/
specialists/.staging/
!.gitignore
"""


def _run(cwd: str, args: Sequence[str], *, check: bool = True) -> str:
    """Run ``git`` under *cwd*. Returns stripped stdout."""
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=check,
        capture_output=True, text=True,
    )
    return completed.stdout.strip()


def init_repo(config_dir: str) -> None:
    """Initialize *config_dir* as a local git repo if not already one.

    Idempotent: on an already-initialized repo the only action is the
    ``.gitignore`` reconcile below. Writes ``.gitignore`` to restrict
    tracking to ``agents/``, ``policies/``, ``schema/``, and the user
    marketplace manifest. Makes one initial commit so ``HEAD`` resolves.
    """
    gitignore = os.path.join(config_dir, ".gitignore")

    if os.path.isdir(os.path.join(config_dir, ".git")):
        # P-3 (v0.69.1): existing deployments carry the whitelist their repo
        # was initialized with — reconcile .gitignore on every boot so
        # whitelist changes (e.g. marketplace.json) reach them without a
        # fresh install. snapshot_manual_edits runs right after and commits
        # any newly-tracked files as the boot snapshot.
        try:
            with open(gitignore, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = ""
        if current != _GITIGNORE_CONTENT:
            logger.info("Refreshing config-repo .gitignore whitelist")
            with open(gitignore, "w", encoding="utf-8") as fh:
                fh.write(_GITIGNORE_CONTENT)
            _run(config_dir, ["add", ".gitignore"], check=False)
            _run(config_dir, ["commit", "-qm",
                              "update .gitignore whitelist"], check=False)
        return

    logger.info("Initializing config git repo at %s", config_dir)
    _run(config_dir, ["init", "-q"])
    _run(config_dir, ["config", "user.email", "casa-agent@local"])
    _run(config_dir, ["config", "user.name",  "Casa Agent"])

    with open(gitignore, "w", encoding="utf-8") as fh:
        fh.write(_GITIGNORE_CONTENT)

    # add -A honors the .gitignore whitelist and — unlike explicit pathspecs —
    # cannot abort the whole add when a whitelisted dir doesn't exist yet
    # (git rejects unmatched pathspecs wholesale; marketplace/ is absent on a
    # fresh install).
    _run(config_dir, ["add", "-A"], check=False)
    _run(config_dir, ["commit", "-qm", "initial config snapshot"],
         check=False)


def commit_config(config_dir: str, message: str) -> str:
    """Stage + commit any tracked-file changes. Returns the new sha, or
    an empty string if there were no changes to commit.
    """
    status = _run(config_dir, ["status", "--porcelain"])
    if not status:
        return ""

    _run(config_dir, ["add", "-A"])
    _run(config_dir, ["commit", "-qm", message])
    return _run(config_dir, ["rev-parse", "HEAD"])


def changed_paths(config_dir: str, sha: str) -> list[str]:
    """Return the repo-relative paths a commit touched (vs its first parent).

    Used by the G-2 reload guard (#231/#222) to tell a plugin-registry-only
    persist commit — already activated in-process — from a commit that also
    edits agents/ or policies/ and therefore genuinely owes a reload. Returns
    an empty list on any git error (fail-safe: the caller then arms the reload
    obligation as usual rather than wrongly suppressing it).
    """
    try:
        out = _run(config_dir,
                   ["diff-tree", "--no-commit-id", "--name-only", "-r", sha])
    except Exception:  # noqa: BLE001 — never let a git hiccup break a commit
        return []
    return [line for line in out.splitlines() if line.strip()]


def snapshot_manual_edits(config_dir: str) -> str | None:
    """Commit any uncommitted changes found in tracked paths. Returns
    the new sha if a commit was made, else None.

    Runs at Casa boot so human edits via SSH land as proper commits
    before the builder agent can race against them.
    """
    status = _run(config_dir, ["status", "--porcelain"])
    if not status:
        return None
    _run(config_dir, ["add", "-A"])
    _run(config_dir, ["commit", "-qm", "manual edit (boot-time snapshot)"])
    return _run(config_dir, ["rev-parse", "HEAD"])


def restore_file(config_dir: str, sha: str, relpath: str) -> None:
    """Restore *relpath* to its content at *sha* and commit the restore."""
    _run(config_dir, ["checkout", sha, "--", relpath])
    _run(config_dir, ["add", relpath])
    _run(config_dir, ["commit", "-qm", f"restore {relpath} to {sha[:8]}"])
