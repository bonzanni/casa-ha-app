"""Agent-home directory provisioning for in_casa agents.

Creates /addon_configs/casa-agent/agent-home/<role>/.claude/settings.json
with `enabledPlugins` seeded from defaults/agents/<role>/plugins.yaml.
Idempotent — preserves user-added entries (P-3 drift policy).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from plugins_config import load_plugins_yaml

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
    if not isinstance(existing.get("enabledPlugins"), dict):
        existing["enabledPlugins"] = {}

    # Apply default seeding (plugins.yaml entries become True; user edits preserved).
    plugins_yaml = defaults_root / "defaults" / "agents" / role / "plugins.yaml"
    cfg = load_plugins_yaml(plugins_yaml)
    for ref in cfg.iter_refs():
        existing["enabledPlugins"].setdefault(ref, True)

    # Write back.
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    logger.info("agent-home provisioned: role=%s settings=%s", role, settings_path)
