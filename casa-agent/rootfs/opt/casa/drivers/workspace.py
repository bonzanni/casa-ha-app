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

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "engagement_run_template.sh",
)

# v0.64.0: single owner of the per-engagement log location. The s6-log run
# script (render_log_run_script), the driver's DEBUG relay, the retention
# sweep, and the delete_engagement_workspace tool all derive from this —
# moving the location means changing exactly one place.
ENGAGEMENT_LOG_ROOT = "/var/log"


def engagement_log_dir(engagement_id: str, *, root: str | None = None) -> str:
    """Absolute path of the engagement's s6-log directory."""
    return os.path.join(
        root if root is not None else ENGAGEMENT_LOG_ROOT,
        f"casa-engagement-{engagement_id}",
    )

# Bug 5 (v0.14.6): env-var names must match shell-identifier syntax.
# Pre-fix the key was interpolated unsanitised into `export {}='{}'`,
# so a key containing "\n" or other shell-special chars escaped the
# export line and ran arbitrary commands. Same upper-snake convention
# as ``plugin_env_conf._VAR_NAME_RE``.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# L-1 (v0.34.2): valid CC permission patterns we forward into
# engagement-scoped .claude/settings.json::permissions.allow.
# Anything else (e.g. Casa-internal tool names) is dropped with a WARNING.
# v0.46.4: accept BARE ``Bash`` (broad, no parens — for dev executors that must
# run open-ended toolchains; safety stays in the hook stack: block_dangerous_bash
# + path_scope + the engagement_permission_relay) and ``WebFetch``/``WebSearch``.
_VALID_CC_PERMISSION_RE = re.compile(
    r"^(Bash(\(.+\))?|Read|Write|Edit|Glob|Grep|Skill|WebFetch|WebSearch|mcp__.+)$"
)


def _build_cc_permissions(defn) -> dict:
    """Build CC permissions block for engagement settings.json from ExecutorDefinition.

    Filters ``defn.tools_allowed`` to entries matching valid CC permission
    patterns; non-matching entries (e.g. Casa-internal tool names) are
    dropped with a WARNING. ``permission_mode`` falls through to
    ``"acceptEdits"`` when empty (matches ExecutorDefinition default).
    """
    allow: list[str] = []
    for entry in defn.tools_allowed:
        if _VALID_CC_PERMISSION_RE.match(entry):
            allow.append(entry)
        else:
            logger.warning(
                "executor %r: dropping tools_allowed entry %r — "
                "not a valid CC permission pattern",
                defn.type, entry,
            )
    return {"allow": allow, "defaultMode": defn.permission_mode or "acceptEdits"}


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
    plugin_dirs: list[str] | None = None,
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

    # §3.8: immutable plugin-artifact paths → repeated --plugin-dir flags.
    # Each is an absolute store path (same validation as extra_dirs, minus
    # the engagement-relative prefixing).
    for d in (plugin_dirs or []):
        _validate_extra_dir(d)
    plugin_dir_flags = " ".join(
        f"--plugin-dir {shlex.quote(d)}" for d in (plugin_dirs or []))

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
        .replace("{PLUGIN_DIR_FLAGS}", plugin_dir_flags)
        .replace("{EXTRA_UNSET}", extra_unset_str)
        .replace("{EXTRA_EXPORT}", export_lines)
    )


def render_log_run_script(*, engagement_id: str) -> str:
    """Render an s6-log run script for an engagement's stdout capture.

    The resulting script routes the engagement service's stdout to
    <ENGAGEMENT_LOG_ROOT>/casa-engagement-<id>/current, rotating at 1MB with
    up to 20 archive files. This is consumed by
    drivers.claude_code_driver._relay_log_lines via readline tailing.
    """
    log_dir = engagement_log_dir(engagement_id)
    return (
        "#!/command/with-contenv sh\n"
        "set -e\n"
        f"mkdir -p {log_dir}\n"
        f"exec s6-log n20 s1000000 {log_dir}\n"
    )


async def provision_workspace(
    *,
    engagements_root: str,
    engagement_id: str,
    defn,                                    # ExecutorDefinition
    task: str,
    context: str,
    casa_framework_mcp_url: str,
    workspace_template_root: Path | None = None,
    world_state_summary: str = "",
    executor_memory: str = "",
) -> str:
    """Create /<engagements_root>/<id>/ with the full provisioning tree.

    Returns the absolute workspace path. Caller must NOT create the
    directory first — this function does.

    If ``workspace_template_root`` is provided and the template directory
    exists, ``render_workspace_template`` populates CLAUDE.md and
    .claude/settings.json — independent of plugin assignment (§3.3; plugins
    now load via --plugin-dir, not settings.json). Otherwise the legacy
    prompt-interpolation path is used.

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

    # L-1 (v0.34.2): both legacy and template paths need hooks_yaml_data.
    # Load once here; render_workspace_template + legacy branch share it.
    hooks_yaml_data: dict = {}
    if getattr(defn, "hooks_path", None) and os.path.isfile(defn.hooks_path):
        with open(defn.hooks_path, "r", encoding="utf-8") as fh:
            hooks_yaml_data = yaml.safe_load(fh) or {}

    # Plan 4b §16.3: if a workspace-template exists for this executor, render it
    # into the workspace root. This subsumes the old symlink-loop behavior.
    # §3.3: selection is independent of plugin assignment now.
    if (
        workspace_template_root is not None
        and workspace_template_root.is_dir()
    ):
        render_workspace_template(
            template_root=workspace_template_root,
            dest=ws,
            defn=defn,
            hooks_yaml_data=hooks_yaml_data,
            executor_type=defn.type,
            task=task,
            context=context,
            world_state_summary=world_state_summary,
            executor_memory=executor_memory,
        )
    else:
        # 1. CLAUDE.md — the executor prompt, interpolated (legacy path).
        prompt_text = _read_text(defn.prompt_template_path)
        prompt_interpolated = (
            prompt_text
            .replace("{task}", task or "")
            .replace("{context}", context or "(none)")
            .replace("{executor_type}", defn.type)
            .replace("{executor_memory}", executor_memory or "")
        )
        (ws / "CLAUDE.md").write_text(prompt_interpolated, encoding="utf-8")

        # .claude/settings.json with translated hooks (legacy path).
        (ws / ".claude").mkdir(exist_ok=True)
        settings = translate_hooks_to_settings(
            hooks_yaml_data, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        # L-1 (v0.34.2): merge permissions block from defn alongside hooks.
        settings["permissions"] = _build_cc_permissions(defn)
        (ws / ".claude" / "settings.json").write_text(
            json.dumps(settings, indent=2), encoding="utf-8",
        )

    # W3 (Task 8): cache the fetched executor_memory block at <ws>/.executor_memory
    # so a later ``refresh_claude_md`` (boot replay) can re-interpolate the SAME
    # {executor_memory} section — the block is a LIVE Hindsight fetch, not
    # re-derivable at boot, so it must be persisted alongside the workspace.
    (ws / ".executor_memory").write_text(executor_memory or "", encoding="utf-8")

    # v0.74.2 (live finding 2026-07-13): provision the executor's doctrine/
    # into the workspace — the rendered CLAUDE.md references doctrine/*.md,
    # which never existed in claude_code workspaces (the plugin-developer
    # read missing files and proceeded without its conventions). Copy, not
    # symlink: the workspace must stay self-contained + immutable-ish even
    # if the live /config doctrine changes mid-engagement. FAIL CLOSED on a
    # declared-but-missing source (Sol design review: silently proceeding
    # recreates the original degradation); a doctrine-less executor opts out
    # with an explicitly empty `doctrine_dir:` in its definition.yaml.
    doctrine_src = getattr(defn, "doctrine_dir", "") or ""
    if doctrine_src:
        if not os.path.isdir(doctrine_src):
            raise FileNotFoundError(
                f"executor {defn.type!r} declares doctrine at "
                f"{doctrine_src!r} but the directory is missing — refusing "
                "to provision a workspace whose CLAUDE.md references absent "
                "doctrine (set doctrine_dir: '' to opt out)")
        shutil.copytree(doctrine_src, ws / "doctrine")

    # Per-engagement HOME dir (plugins symlinks removed in v0.14.x).
    # L-1 (v0.34.2): hoisted outside the if/else so template path also gets it.
    (ws / ".home" / ".claude" / "plugins").mkdir(parents=True)

    # 2. .mcp.json — point at Casa's MCP HTTP bridge with engagement id header.
    mcp_config = {"mcpServers": {
        "casa-framework": {
            "type": "http",
            "url": casa_framework_mcp_url,
            "headers": {"X-Casa-Engagement-Id": engagement_id},
        },
        # E-12 (v0.37.0): per-engagement stdio channel server for
        # operator UX (reply, ask, set_progress, permission relay).
        # Spec: docs/superpowers/specs/2026-05-12-e12-claude_code-channels.md.
        "casa-engagement-channel": {
            "command": "/opt/casa/venv/bin/python",
            "args": [
                "/opt/casa/channels/casa_engagement_channel.py",
                "--engagement-id", engagement_id,
            ],
            "env": {
                "CASA_INTERNAL_SOCKET": "/run/casa/internal.sock",
            },
        },
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


def refresh_claude_md(ws_dir: str, *, defn, rec) -> None:
    """Re-render an existing workspace's CLAUDE.md from the engagement record
    (W3/Sol r8-B5, Task 8).

    Boot replay calls this for EVERY resumed brief-bearing engagement so the
    workspace CLAUDE.md is re-derived from the VERBATIM ``origin["brief"]``
    (per design §211 the resume path re-renders from the raw brief; a persisted
    derived form could go stale). Runs the SAME whole-file interpolation as
    provisioning — the same template-vs-legacy CHOICE, and every pre-existing
    placeholder section ({context}/{world_state_summary}/{executor_memory}/
    {executor_type}) survives the refresh:

      - ``{task}``               = ``brief_task_for(rec, defn)`` (derives from
        the raw brief; the canonical ``rec.task`` fallback when no brief).
      - ``{context}``            = ``rec.origin.get("context", "")``.
      - ``{world_state_summary}``= ``rec.origin.get("world_state_summary", "")``.
      - ``{executor_type}``      = ``defn.type``.
      - ``{executor_memory}``    = contents of ``<ws>/.executor_memory`` cached
        at provision (absent → "").

    ``rec`` (not just its origin) is required because ``brief_task_for`` needs
    the canonical ``.task`` fallback for a brief-less record. Raises on I/O
    failure — the caller (boot replay) treats a raised refresh as a fail-closed
    refuse-to-resume.
    """
    from drivers.brief import brief_task_for

    ws = Path(ws_dir)
    task = brief_task_for(rec, defn)
    context = rec.origin.get("context", "")
    world_state_summary = rec.origin.get("world_state_summary", "")

    mem_path = ws / ".executor_memory"
    executor_memory = (
        mem_path.read_text(encoding="utf-8") if mem_path.is_file() else ""
    )

    # Same selection as provision (workspace.py:228-253): a workspace-template/
    # beside the prompt template selects the template render path; else legacy.
    exec_dir = Path(defn.prompt_template_path).parent
    template_root = exec_dir / "workspace-template"

    if template_root.is_dir():
        # Mirror render_workspace_template (workspace.py:489-496) EXACTLY.
        text = (template_root / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
        text = (
            text.replace("{executor_type}", defn.type)
                .replace("{task}", task)
                .replace("{context}", context)
                .replace("{world_state_summary}", world_state_summary)
                .replace("{executor_memory}", executor_memory or "")
        )
        (ws / "CLAUDE.md").write_text(text, encoding="utf-8")
    else:
        # Mirror the legacy provision branch (workspace.py:245-253) EXACTLY.
        prompt_text = _read_text(defn.prompt_template_path)
        prompt_interpolated = (
            prompt_text
            .replace("{task}", task or "")
            .replace("{context}", context or "(none)")
            .replace("{executor_type}", defn.type)
            .replace("{executor_memory}", executor_memory or "")
        )
        (ws / "CLAUDE.md").write_text(prompt_interpolated, encoding="utf-8")


def write_casa_meta(
    *, workspace_path: str, engagement_id: str, executor_type: str,
    status: str, created_at: str,
    finished_at: str | None, retention_until: str | None,
    plugin_artifacts: list[dict] | None = None,
) -> None:
    # This dict is reconstructed from scratch on every rewrite — the immutable
    # plugin_artifacts (§3.8) must be re-passed by every caller (initial write
    # + terminal finalize) or it is silently dropped.
    meta = {
        "engagement_id": engagement_id,
        "executor_type": executor_type,
        "status": status,
        "created_at": created_at,
        "finished_at": finished_at,
        "retention_until": retention_until,
        "plugin_artifacts": list(plugin_artifacts or []),
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


async def _sweep_workspaces(
    *, engagements_root: str, log_root: str | None = None,
) -> None:
    """Periodic sweep: delete terminal engagement workspaces past retention.

    Status semantics from .casa-meta.json:
      - UNDERGOING: skip (engagement still running).
      - COMPLETED / CANCELLED: delete iff retention_until <= now.
      - Terminal but retention_until is null: log warning + skip (bug).
      - No .casa-meta.json at all: skip (caller-managed via
        delete_engagement_workspace MCP tool).

    v0.64.0: the per-engagement s6-log dir (<log_root>/casa-engagement-<id>)
    follows the same retention — removed together with the workspace, so
    post-mortem logs stay available exactly as long as the workspace does
    (bounded ~21 MB/engagement by ``s6-log n20 s1000000``).

    Disk-pressure mode (§6.5 aggressive tier) is out of scope — see spec
    §8.3. The N150 has >30 GB free.
    """
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
        except OSError as exc:
            logger.warning(
                "workspace sweep: rmtree %s failed: %s",
                entry.path, exc,
            )
        else:
            logger.info(
                "workspace sweep: removed %s (status=%s, past retention)",
                entry.name, status,
            )
            log_dir = engagement_log_dir(entry.name, root=log_root)
            try:
                if os.path.isdir(log_dir):
                    shutil.rmtree(log_dir)
            except OSError as exc:
                # The workspace is already gone, so no future sweep can map
                # to this log dir again — warn, it's the only signal left.
                logger.warning(
                    "workspace sweep: log dir rmtree %s failed: %s",
                    log_dir, exc,
                )


# ---------------------------------------------------------------------------
# Workspace-template rendering — §16.3 of Plan 4b.
# ---------------------------------------------------------------------------


def render_workspace_template(
    *,
    template_root: Path,
    dest: Path,
    defn,                                    # ExecutorDefinition (Plan 4b §16.3 + L-1)
    hooks_yaml_data: dict,                   # L-1 (v0.34.2)
    executor_type: str,
    task: str,
    context: str,
    world_state_summary: str,
    executor_memory: str = "",
) -> None:
    """Copy the executor's workspace-template/ subtree into `dest`, interpolate
    CLAUDE.md.tmpl → CLAUDE.md, and generate .claude/settings.json with hooks +
    permissions from the executor definition. Plan 4b §16.3.

    §3.3 (unified plugin arch): settings.json NO LONGER carries enabledPlugins —
    executor plugins load via the pinned --plugin-dir flags on the run script.

    L-1 (v0.34.2): ``defn`` and ``hooks_yaml_data`` are REQUIRED. The generated
    settings.json always includes a ``hooks`` block (from
    ``translate_hooks_to_settings``) and a ``permissions`` block (from
    ``_build_cc_permissions``).
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
                .replace("{executor_memory}", executor_memory or "")
        )
        (dest / "CLAUDE.md").write_text(text, encoding="utf-8")

    # Generate .claude/settings.json (hooks + permissions only).
    # §3.3: no enabledPlugins — executor plugins load via --plugin-dir.
    claude_dir = dest / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    hooks_block = translate_hooks_to_settings(
        hooks_yaml_data, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
    )
    settings = {
        "hooks": hooks_block.get("hooks", {}),
        "permissions": _build_cc_permissions(defn),
    }
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
