"""Per-engagement workspace provisioner for the claude_code driver."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import time
from pathlib import Path

import yaml

from drivers.hook_bridge import translate_hooks_to_settings
from plugins_config import load_plugins_yaml

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "engagement_run_template.sh",
)

# Bug 5 (v0.14.6): env-var names must match shell-identifier syntax.
# Pre-fix the key was interpolated unsanitised into `export {}='{}'`,
# so a key containing "\n" or other shell-special chars escaped the
# export line and ran arbitrary commands. Same upper-snake convention
# as ``plugin_env_conf._VAR_NAME_RE``.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class WorkspaceConfigError(ValueError):
    """Raised when ExecutorDefinition values would shell-inject the run script."""


def _validate_extra_dir(d: str) -> None:
    """Reject extra_dir entries that aren't usable absolute paths.

    Bug 4 (v0.14.6): pre-fix any string was interpolated unquoted into
    `--add-dir <d>`. Strings with spaces, newlines, semicolons, or
    quotes injected shell. We now require an absolute POSIX path with
    no shell-special characters; values still get shlex.quote'd for
    belt-and-braces.
    """
    if not isinstance(d, str) or not d:
        raise WorkspaceConfigError(f"extra_dir must be a non-empty string: {d!r}")
    if not d.startswith("/"):
        raise WorkspaceConfigError(
            f"extra_dir must be an absolute path (start with '/'): {d!r}"
        )
    if any(c in d for c in "\n\r\0;|&`$<>'\""):
        raise WorkspaceConfigError(
            f"extra_dir contains shell-special characters: {d!r}"
        )


def render_run_script(
    *, engagement_id: str, permission_mode: str,
    extra_dirs: list[str], extra_unset: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Read the run-script template and substitute per-engagement values.

    The per-engagement workspace is always included in --add-dir; any
    caller-provided extras are appended after it.

    ``extra_dirs`` — each element MUST be an absolute path with no
    shell-special characters. Each value is also shlex.quote'd before
    interpolation so a stricter validator can be added later without
    re-checking quoting (Bug 4, v0.14.6).

    ``extra_env`` — optional mapping of env var name → value to export
    inside the run script. Names must match ``[A-Za-z_][A-Za-z0-9_]*``
    (rejected at render time). Values are single-quote-escaped via the
    standard ``'\\''`` idiom (Bug 5, v0.14.6).
    """
    with open(_TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template = fh.read()

    for d in extra_dirs:
        _validate_extra_dir(d)
    all_dirs = [f"/data/engagements/{engagement_id}/", *extra_dirs]
    add_dir_flags = " ".join(f"--add-dir {shlex.quote(d)}" for d in all_dirs)

    extra_unset_str = " ".join(extra_unset or [])

    if extra_env:
        bad = [k for k in extra_env if not _ENV_VAR_NAME_RE.match(str(k))]
        if bad:
            raise WorkspaceConfigError(
                f"extra_env keys must match [A-Za-z_][A-Za-z0-9_]*; "
                f"got invalid: {bad!r}"
            )
        export_lines = "\n".join(
            "export {}='{}'".format(k, str(v).replace("'", "'\\''"))
            for k, v in extra_env.items()
        )
    else:
        export_lines = ""

    return (
        template
        .replace("{ID_SHORT}", engagement_id[:8])
        .replace("{ID}", engagement_id)
        .replace("{PERMISSION_MODE}", permission_mode)
        .replace("{ADD_DIR_FLAGS}", add_dir_flags)
        .replace("{EXTRA_UNSET}", extra_unset_str)
        .replace("{EXTRA_EXPORT}", export_lines)
    )


def render_log_run_script(*, engagement_id: str) -> str:
    """Render an s6-log run script for an engagement's stdout capture.

    The resulting script routes the engagement service's stdout to
    /var/log/casa-engagement-<id>/current, rotating at 1MB with up to 20
    archive files. This is consumed by drivers.claude_code_driver._capture_url
    via readline tailing.
    """
    return (
        "#!/command/with-contenv sh\n"
        "set -e\n"
        f"mkdir -p /var/log/casa-engagement-{engagement_id}\n"
        f"exec s6-log n20 s1000000 /var/log/casa-engagement-{engagement_id}\n"
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
    workspace_template_root: Path | None = None,
    plugins_yaml: Path | None = None,
    world_state_summary: str = "",
) -> str:
    """Create /<engagements_root>/<id>/ with the full provisioning tree.

    Returns the absolute workspace path. Caller must NOT create the
    directory first — this function does.

    If ``workspace_template_root`` and ``plugins_yaml`` are both provided
    and the template directory exists, ``render_workspace_template`` is
    called to populate CLAUDE.md and .claude/settings.json (Plan 4b §16.3).
    Otherwise the legacy prompt-interpolation path is used.

    Note: filesystem I/O (mkdir, write_text, os.symlink, os.mkfifo) is
    currently synchronous despite the ``async def`` surface. The cost
    per engagement-start (one-time provisioning of a few files + one
    FIFO) is well under 10ms on the N150, so the brief event-loop stall
    is acceptable. If profiling later shows otherwise, wrap the filesystem
    calls in ``asyncio.to_thread`` to match the pattern in
    ``drivers/s6_rc.py``.
    """
    ws = Path(engagements_root) / engagement_id
    ws.mkdir(parents=True, exist_ok=False)

    # Plan 4b §16.3: if a workspace-template exists for this executor, render it
    # into the workspace root. This subsumes the old symlink-loop behavior.
    if (
        workspace_template_root is not None
        and plugins_yaml is not None
        and workspace_template_root.is_dir()
    ):
        render_workspace_template(
            template_root=workspace_template_root,
            plugins_yaml=plugins_yaml,
            dest=ws,
            executor_type=defn.type,
            task=task,
            context=context,
            world_state_summary=world_state_summary,
        )
    else:
        # 1. CLAUDE.md — the executor prompt, interpolated (legacy path).
        prompt_text = _read_text(defn.prompt_template_path)
        prompt_interpolated = (
            prompt_text
            .replace("{task}", task or "")
            .replace("{context}", context or "(none)")
            .replace("{executor_type}", defn.type)
        )
        (ws / "CLAUDE.md").write_text(prompt_interpolated, encoding="utf-8")

        # .claude/settings.json with translated hooks (legacy path).
        (ws / ".claude").mkdir(exist_ok=True)
        hooks_yaml_data: dict = {}
        if getattr(defn, "hooks_path", None) and os.path.isfile(defn.hooks_path):
            with open(defn.hooks_path, "r", encoding="utf-8") as fh:
                hooks_yaml_data = yaml.safe_load(fh) or {}
        settings = translate_hooks_to_settings(
            hooks_yaml_data, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        (ws / ".claude" / "settings.json").write_text(
            json.dumps(settings, indent=2), encoding="utf-8",
        )

        # Per-engagement HOME dir (plugins symlinks removed in v0.14.x).
        (ws / ".home" / ".claude" / "plugins").mkdir(parents=True)

    # 2. .mcp.json — point at Casa's MCP HTTP bridge with engagement id header.
    mcp_config = {"mcpServers": {
        "casa-framework": {
            "type": "http",
            "url": casa_framework_mcp_url,
            "headers": {"X-Casa-Engagement-Id": engagement_id},
        }
    }}
    (ws / ".mcp.json").write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")

    # 3. Named FIFO for stdin.
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
    except json.JSONDecodeError:
        logger.warning(
            "load_casa_meta: %s is not valid JSON — treating as absent", path,
        )
        return None
    except OSError:
        logger.warning(
            "load_casa_meta: I/O error reading %s", path, exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Workspace sweeper — §6.5 of Plan 4a (Plan 4a.1 delivery).
# ---------------------------------------------------------------------------


async def _sweep_workspaces(*, engagements_root: str) -> None:
    """Periodic sweep: delete terminal engagement workspaces past retention.

    Status semantics from .casa-meta.json:
      - UNDERGOING: skip (engagement still running).
      - COMPLETED / CANCELLED: delete iff retention_until <= now.
      - Terminal but retention_until is null: log warning + skip (bug).
      - No .casa-meta.json at all: skip (caller-managed via
        delete_engagement_workspace MCP tool).

    Disk-pressure mode (§6.5 aggressive tier) is out of scope — see spec
    §8.3. The N150 has >30 GB free.
    """
    import shutil

    if not os.path.isdir(engagements_root):
        return

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for entry in os.scandir(engagements_root):
        if not entry.is_dir():
            continue
        meta = load_casa_meta(entry.path)
        if meta is None:
            continue
        status = meta.get("status")
        if status == "UNDERGOING":
            continue
        retention_until = meta.get("retention_until")
        if retention_until is None:
            logger.warning(
                "workspace sweep: engagement %s has terminal status %r "
                "but retention_until is null; skipping",
                entry.name, status,
            )
            continue
        if retention_until > now_iso:
            continue
        try:
            shutil.rmtree(entry.path)
            logger.info(
                "workspace sweep: removed %s (status=%s, past retention)",
                entry.name, status,
            )
        except OSError as exc:
            logger.warning(
                "workspace sweep: rmtree %s failed: %s",
                entry.path, exc,
            )


# ---------------------------------------------------------------------------
# Workspace-template rendering — §16.3 of Plan 4b.
# ---------------------------------------------------------------------------


def render_workspace_template(
    *,
    template_root: Path,
    plugins_yaml: Path,
    dest: Path,
    executor_type: str,
    task: str,
    context: str,
    world_state_summary: str,
) -> None:
    """Copy the executor's workspace-template/ subtree into `dest`, interpolate
    CLAUDE.md.tmpl → CLAUDE.md, and generate .claude/settings.json with
    enabledPlugins from plugins.yaml. Plan 4b §16.3.
    """
    if not template_root.is_dir():
        raise FileNotFoundError(f"workspace template missing: {template_root}")

    dest.mkdir(parents=True, exist_ok=True)

    # Copy every file under template_root except CLAUDE.md.tmpl (handled below).
    for src in template_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_root)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.name == "CLAUDE.md.tmpl":
            continue
        shutil.copy2(src, target)

    # Interpolate CLAUDE.md.
    tmpl = template_root / "CLAUDE.md.tmpl"
    if tmpl.is_file():
        text = tmpl.read_text(encoding="utf-8")
        text = (
            text.replace("{executor_type}", executor_type)
                .replace("{task}", task)
                .replace("{context}", context)
                .replace("{world_state_summary}", world_state_summary)
        )
        (dest / "CLAUDE.md").write_text(text, encoding="utf-8")

    # Generate .claude/settings.json::enabledPlugins from plugins.yaml.
    claude_dir = dest / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    cfg = load_plugins_yaml(plugins_yaml)
    enabled = {ref: True for ref in cfg.iter_refs()}
    settings_path.write_text(
        json.dumps({"enabledPlugins": enabled}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
