"""Git-backed history for ``/addon_configs/casa-agent/``.

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
!schema/
!schema/**
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

    Idempotent: the second call is a no-op (``.git`` already exists).
    Writes ``.gitignore`` to restrict tracking to ``agents/``, ``policies/``,
    and ``schema/``. Makes one initial commit so ``HEAD`` resolves.
    """
    if os.path.isdir(os.path.join(config_dir, ".git")):
        return

    logger.info("Initializing config git repo at %s", config_dir)
    _run(config_dir, ["init", "-q"])
    _run(config_dir, ["config", "user.email", "casa-agent@local"])
    _run(config_dir, ["config", "user.name",  "Casa Agent"])

    gitignore = os.path.join(config_dir, ".gitignore")
    with open(gitignore, "w", encoding="utf-8") as fh:
        fh.write(_GITIGNORE_CONTENT)

    _run(config_dir, ["add", ".gitignore", "agents", "policies", "schema"],
         check=False)  # dirs may be empty on fresh install
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
