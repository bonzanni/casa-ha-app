"""Per-engagement workspace provisioner for the claude_code driver."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "engagement_run_template.sh",
)


def render_run_script(
    *, engagement_id: str, permission_mode: str,
    extra_dirs: list[str], extra_unset: list[str] | None = None,
) -> str:
    """Read the run-script template and substitute per-engagement values.

    The per-engagement workspace is always included in --add-dir; any
    caller-provided extras are appended after it.
    """
    with open(_TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template = fh.read()

    all_dirs = [f"/data/engagements/{engagement_id}/", *extra_dirs]
    add_dir_flags = " ".join(f"--add-dir {d}" for d in all_dirs)

    extra_unset_str = " ".join(extra_unset or [])

    return (
        template
        .replace("{ID_SHORT}", engagement_id[:8])
        .replace("{ID}", engagement_id)
        .replace("{PERMISSION_MODE}", permission_mode)
        .replace("{ADD_DIR_FLAGS}", add_dir_flags)
        .replace("{EXTRA_UNSET}", extra_unset_str)
    )
