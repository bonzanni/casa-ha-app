"""Agent-home directory provisioning for in_casa agents.

Creates /config/agent-home/<role>/.claude/settings.json (for hooks + user
edits). Idempotent — preserves user-added entries (P-3 drift policy).

Under the unified plugin architecture (v0.71.0), plugin ASSIGNMENT lives in
the registry, not in per-agent-home ``enabledPlugins`` — this module no longer
seeds it. A pre-existing ``enabledPlugins`` key (from an older deploy) is left
untouched (user data is never deleted); nothing reads it anymore.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def provision_agent_home(
    *,
    role: str,
    home_root: Path | str,
    defaults_root: Path | str,
) -> None:
    home_root = Path(home_root)
    defaults_root = Path(defaults_root)

    agent_dir = home_root / role
    claude_dir = agent_dir / ".claude"
    settings_path = claude_dir / "settings.json"

    # Load existing settings (preserve user edits).
    existing: dict = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("settings.json at %s is not valid JSON — recreating", settings_path)
            existing = {}
    if not isinstance(existing, dict):
        logger.warning(
            "settings.json at %s is not a JSON object — recreating",
            settings_path,
        )
        existing = {}

    # v0.71.0: no enabledPlugins seeding — plugin assignment is the registry's
    # job. A stale key from an older deploy is preserved (never deleted).

    # Write back.
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    logger.info("agent-home provisioned: role=%s settings=%s", role, settings_path)


def provision_all_homes(
    *,
    role_configs: dict,
    specialist_configs: dict,
    home_root: Path | str,
    defaults_root: Path | str,
) -> None:
    """Provision an agent-home for every in_casa resident or specialist.

    Iterates the union of `role_configs` and `specialist_configs`,
    delegating each role to ``provision_agent_home``. Idempotent — safe
    to call on every boot.

    Executors are deliberately excluded: they run with
    ``cwd=/config`` per
    ``tools.py::_build_executor_options``, not from an
    ``agent-home/<role>/`` directory. Adding executors here would create
    empty unused dirs.

    Each role's provisioning is wrapped in its own try/except so a
    single malformed plugins.yaml cannot take down the boot — the
    failing role is logged at WARNING and skipped; the rest continue.
    """
    for role in {**role_configs, **specialist_configs}:
        try:
            provision_agent_home(
                role=role, home_root=home_root, defaults_root=defaults_root,
            )
        except Exception as exc:  # noqa: BLE001 — boot non-fatal; isolates one role from others
            logger.warning(
                "agent-home provisioning failed for role=%s: %s", role, exc,
            )
