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
) -> list[dict[str, str]]:
    """Return the SDK `plugins=[...]` list for a given HOME + cache + seed combo.

    Failure policy: on CLI error, log a warning and return an empty list.
    Agents still boot; they just miss plugin capabilities until the next
    SDK-client construction (or a successful `casa_reload()`).
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

    plugins: list[dict[str, str]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
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
