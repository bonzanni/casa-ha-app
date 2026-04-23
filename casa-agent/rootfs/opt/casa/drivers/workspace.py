"""Per-engagement workspace provisioner for the claude_code driver."""

from __future__ import annotations

import json
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


async def provision_workspace(
    *,
    engagements_root: str,
    base_plugins_root: str,
    engagement_id: str,
    defn,                                    # ExecutorDefinition
    task: str,
    context: str,
    casa_framework_mcp_url: str,
) -> str:
    """Create /<engagements_root>/<id>/ with the full provisioning tree.

    Returns the absolute workspace path. Caller must NOT create the
    directory first — this function does.
    """
    ws = Path(engagements_root) / engagement_id
    ws.mkdir(parents=True, exist_ok=False)

    # 1. CLAUDE.md — the executor prompt, interpolated.
    prompt_text = _read_text(defn.prompt_template_path)
    prompt_interpolated = (
        prompt_text
        .replace("{task}", task or "")
        .replace("{context}", context or "(none)")
        .replace("{executor_type}", defn.type)
    )
    (ws / "CLAUDE.md").write_text(prompt_interpolated, encoding="utf-8")

    # 2. .mcp.json — point at Casa's in-process casa-framework MCP server.
    mcp_config = {"mcpServers": {
        "casa-framework": {
            "type": "http",
            "url": casa_framework_mcp_url,
        }
    }}
    (ws / ".mcp.json").write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")

    # 3. .claude/settings.json — written by hook_bridge in its own pass;
    #    provision a stub here so the dir exists.
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {}}, indent=2), encoding="utf-8",
    )

    # 4. Per-engagement HOME + plugin symlinks.
    plugins_dst = ws / ".home" / ".claude" / "plugins"
    plugins_dst.mkdir(parents=True)

    # Tier 1 — baseline symlinks first.
    base_root = Path(base_plugins_root)
    if base_root.is_dir():
        for pack in sorted(base_root.iterdir()):
            if pack.is_dir():
                os.symlink(str(pack), str(plugins_dst / pack.name))

    # Tier 2 — per-executor packs, force-override any Tier 1 pack of the same name.
    if defn.plugins_dir:
        pe_root = Path(defn.plugins_dir)
        if pe_root.is_dir():
            for pack in sorted(pe_root.iterdir()):
                if not pack.is_dir():
                    continue
                link = plugins_dst / pack.name
                if link.exists() or link.is_symlink():
                    link.unlink()
                os.symlink(str(pack), str(link))

    # 5. Named FIFO for stdin.
    fifo_path = ws / "stdin.fifo"
    os.mkfifo(str(fifo_path), 0o600)

    logger.info("Provisioned workspace for engagement %s at %s",
                engagement_id[:8], ws)
    return str(ws)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_casa_meta(
    *, workspace_path: str, engagement_id: str, executor_type: str,
    status: str, created_at: str,
    finished_at: str | None, retention_until: str | None,
) -> None:
    meta = {
        "engagement_id": engagement_id,
        "executor_type": executor_type,
        "status": status,
        "created_at": created_at,
        "finished_at": finished_at,
        "retention_until": retention_until,
    }
    path = Path(workspace_path) / ".casa-meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_casa_meta(workspace_path: str) -> dict | None:
    path = Path(workspace_path) / ".casa-meta.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
