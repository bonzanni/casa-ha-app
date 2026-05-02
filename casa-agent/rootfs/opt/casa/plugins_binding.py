"""Casa-side plugin binding layer (Plan 4b §16.1 + spike §Key learning 1).

The Claude Agent SDK does NOT auto-consume plugins from settings.json —
it expects a `plugins=[{"type":"local","path":...}]` list built from
absolute cache paths. The CC CLI's `claude plugin list --json` output
already exposes `installPath` per installed plugin, so the binding layer
is a thin shell-out rather than manual cache-layout assembly.

Call this from every ClaudeAgentOptions construction site (residents in
agent.py, specialists + executors in tools.py). Rebuilt per client
construction; `casa_reload()` tears down + rebuilds SDK clients, which
picks up plugin changes for free.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class BindingError(RuntimeError):
    """Raised when `claude plugin list --json` returns unparseable output."""


def build_sdk_plugins(
    *,
    home: Path | str,
    shared_cache: Path | str,
    seed: Path | str,
    role: str | None = None,
) -> list[dict[str, str]]:
    """Return the SDK `plugins=[...]` list for a given HOME + cache + seed combo.

    When ``role`` is provided (residents — agent.py call-site), project-scope
    entries whose ``projectPath`` matches
    ``/addon_configs/casa-agent/agent-home/<role>`` are included regardless
    of the CLI's ``enabled`` field. The CLI evaluates ``enabled`` against
    the calling HOME's settings.json (cc-home), not against the project's
    own ``agent-home/<role>/.claude/settings.json``, so for Casa's
    ``--scope project`` installs the CLI always reports ``enabled: false``
    even when the plugin IS enabled in the agent-home — see O-3.

    When ``role`` is None (specialists + executors call-sites — tools.py),
    project-scope entries are filtered out entirely. This matches the
    install.md doctrine: only residents carry plugins.

    User-scope entries always honour the CLI's ``enabled`` field.

    Failure policy: on CLI error, log a warning and return an empty list.
    Agents still boot; they just miss plugin capabilities until the next
    SDK-client construction (or a successful ``casa_reload()``).
    """
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CODE_PLUGIN_CACHE_DIR": str(shared_cache),
        "CLAUDE_CODE_PLUGIN_SEED_DIR": str(seed),
    }
    try:
        result = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "binding layer degraded: claude plugin list --json exit=%s stderr=%s",
            exc.returncode, (exc.stderr or "").strip()[:200],
        )
        return []
    except subprocess.TimeoutExpired:
        logger.warning("binding layer degraded: claude plugin list --json timed out")
        return []
    except FileNotFoundError:
        logger.warning("binding layer degraded: claude CLI not found on PATH")
        return []

    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BindingError(
            f"claude plugin list --json returned unparseable output: {exc}"
        ) from exc

    if not isinstance(entries, list):
        raise BindingError(
            f"claude plugin list --json returned non-list: {type(entries).__name__}"
        )

    expected_project_path = (
        f"/addon_configs/casa-agent/agent-home/{role}" if role else None
    )

    plugins: list[dict[str, str]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        scope = e.get("scope")
        if scope == "project":
            # Project-scope plugins are Casa-installed into agent-home/<role>.
            # Only surface them for residents (role != None) AND only when
            # the projectPath matches the requested role. Cross-role leakage
            # would defeat the per-resident plugin doctrine.
            if expected_project_path is None:
                continue
            if e.get("projectPath") != expected_project_path:
                continue
            # Do NOT check `enabled` — see docstring, the CLI returns False
            # for project-scope plugins when run from cc-home.
        else:
            # User-scope (or unknown scope — legacy default) — honour the
            # CLI's enabled field.
            if not e.get("enabled"):
                continue
        path = e.get("installPath")
        if not isinstance(path, str) or not path:
            logger.warning(
                "binding layer: plugin %r missing installPath — skipping",
                e.get("id"),
            )
            continue
        plugins.append({"type": "local", "path": path})
    return plugins
