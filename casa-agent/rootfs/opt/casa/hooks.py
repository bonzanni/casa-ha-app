"""Safety hooks: command blocking and per-role path-scope enforcement.

The Claude Agent SDK's `HookContext` carries no agent identity (only a reserved
`signal` field). Per-role rules must therefore be bound at hook-registration
time via a closure; see :func:`make_path_scope_hook`.

Payload shape follows the SDK's `PreToolUseHookSpecificOutput`:
``hookEventName`` + ``permissionDecision`` (allow | deny | ask) + reason.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable

# ---------------------------------------------------------------------------
# Forbidden shell patterns
# ---------------------------------------------------------------------------

FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bcurl\b.*-X\s*POST\b"),
    re.compile(r"\bcurl\b.*--data\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bscp\b"),
]

# ---------------------------------------------------------------------------
# Per-role path rules (keyed on AgentConfig.role, not display name)
# ---------------------------------------------------------------------------

AGENT_PATH_RULES: dict[str, list[tuple[frozenset[str], str]]] = {
    "assistant": [
        (frozenset({"Read"}), "addon_configs/"),
        (frozenset({"Read"}), "/config/"),
        (frozenset({"Write"}), "workspace/"),
    ],
    "butler": [
        (frozenset({"Read"}), "workspace/"),
    ],
    "plugin-builder": [
        (frozenset({"Read", "Write"}), "workspace/"),
    ],
}


HookCallback = Callable[
    [dict[str, Any], str | None, dict[str, Any]],
    Awaitable[dict[str, Any] | None],
]


def _deny(reason: str) -> dict[str, Any]:
    """Return a PreToolUse payload that denies the tool call.

    Shape is defined by the SDK's ``PreToolUseHookSpecificOutput``. The older
    ``{"decision": "deny"}`` shape is silently ignored by the CLI Zod
    validator, which is why per-role enforcement appeared broken.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _normalize_path(raw: str) -> str:
    """Resolve ``..`` segments using PurePosixPath splitting (no OS calls)."""
    parts = PurePosixPath(raw).parts
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved and resolved[-1] != "/":
                resolved.pop()
        elif part != ".":
            resolved.append(part)
    return "/".join(resolved)


# ---------------------------------------------------------------------------
# Hook implementations
# ---------------------------------------------------------------------------


async def block_dangerous_commands(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """Block Bash commands that match FORBIDDEN_PATTERNS."""
    tool_name = input_data.get("tool_name", "")
    if tool_name != "Bash":
        return None

    command = input_data.get("tool_input", {}).get("command", "")
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(command):
            return _deny(f"Blocked by safety hook: {pattern.pattern}")
    return None


async def _check_path_scope(
    role: str,
    input_data: dict[str, Any],
) -> dict[str, Any] | None:
    """Check a Read/Write/Edit call against AGENT_PATH_RULES for *role*."""
    tool_name = input_data.get("tool_name", "")
    if tool_name not in ("Read", "Write", "Edit"):
        return None

    rules = AGENT_PATH_RULES.get(role)
    if rules is None:
        # Unknown role -- allow by default. Phase 2 may invert this.
        return None

    raw_path = input_data.get("tool_input", {}).get("file_path", "")
    norm = _normalize_path(raw_path)

    for perms, prefix in rules:
        if tool_name in perms and norm.startswith(prefix):
            return None

    return _deny(
        f"Role '{role}' is not allowed to {tool_name} path '{raw_path}'"
    )


def make_path_scope_hook(role: str) -> HookCallback:
    """Return a PreToolUse hook callback bound to *role*.

    The SDK provides no agent identity inside the hook context, so the role
    must be captured in a closure at registration time.
    """

    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await _check_path_scope(role, input_data)

    return _hook


# ---------------------------------------------------------------------------
# Phase 4.x — parameterized path_scope + HOOK_POLICIES registry.
# ---------------------------------------------------------------------------


class UnknownPolicyError(Exception):
    """Raised when a hooks.yaml policy or parameter is not recognised."""


def make_path_scope_hook_v2(
    *,
    writable: list[str] | None = None,
    readable: list[str] | None = None,
) -> HookCallback:
    """Return a PreToolUse hook that enforces absolute-path prefixes.

    Unlike :func:`make_path_scope_hook`, this variant is parameterized —
    no hardcoded ``AGENT_PATH_RULES`` table. The per-agent ``hooks.yaml``
    supplies the prefix lists.

    ``writable`` applies to Write/Edit. ``readable`` applies to Read/Write/Edit.
    Anything outside the allowed set denies; exact-match or prefix-match.
    """
    writable = [_normalize_path(p) for p in (writable or [])]
    readable = [_normalize_path(p) for p in (readable or [])]

    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        tool_name = input_data.get("tool_name", "")
        if tool_name not in ("Read", "Write", "Edit"):
            return None
        raw = input_data.get("tool_input", {}).get("file_path", "")
        norm = _normalize_path(raw)

        if tool_name in ("Write", "Edit"):
            if not _has_prefix(norm, writable):
                return _deny(
                    f"path_scope: {tool_name} denied — {raw!r} outside "
                    f"writable prefixes {writable}"
                )
        else:  # Read
            if not _has_prefix(norm, readable):
                return _deny(
                    f"path_scope: Read denied — {raw!r} outside "
                    f"readable prefixes {readable}"
                )
        return None

    return _hook


def _has_prefix(norm: str, prefixes: list[str]) -> bool:
    return any(norm == p or norm.startswith(p.rstrip("/") + "/")
               for p in prefixes)


# HOOK_POLICIES — name → (HookMatcher-factory that takes kwargs and returns
# HookMatcher). The agent_loader resolves `hooks.yaml::pre_tool_use` entries
# through this table.

def _policy_block_dangerous_bash(**kwargs: Any):
    from claude_agent_sdk import HookMatcher
    if kwargs:
        raise UnknownPolicyError(
            f"block_dangerous_bash takes no parameters; got {list(kwargs)}"
        )
    return HookMatcher(matcher="Bash", hooks=[block_dangerous_commands])


def _policy_path_scope(**kwargs: Any):
    from claude_agent_sdk import HookMatcher
    writable = kwargs.pop("writable", None)
    readable = kwargs.pop("readable", None)
    if kwargs:
        raise UnknownPolicyError(
            f"path_scope: unknown parameter(s) {list(kwargs)}; "
            f"supported: writable, readable"
        )
    return HookMatcher(
        matcher="Read|Write|Edit",
        hooks=[make_path_scope_hook_v2(writable=writable, readable=readable)],
    )


HOOK_POLICIES: dict[str, Callable[..., Any]] = {
    "block_dangerous_bash": _policy_block_dangerous_bash,
    "path_scope":           _policy_path_scope,
}


def resolve_hooks(
    config: "HooksConfig",   # forward-ref — imported lazily below
    *,
    default_cwd: str,
) -> dict[str, list[Any]]:
    """Turn a HooksConfig into ``{"PreToolUse": [HookMatcher, ...]}``.

    When ``config.pre_tool_use`` is empty, the default policy bundle is
    applied: ``block_dangerous_bash`` + ``path_scope`` scoped to the
    agent's ``default_cwd``. When it is non-empty, every entry is
    resolved through :data:`HOOK_POLICIES` and unknown policy names
    raise :class:`UnknownPolicyError`.
    """
    matchers: list[Any] = []
    entries = list(config.pre_tool_use or [])

    if not entries:
        entries = [
            {"policy": "block_dangerous_bash"},
            {"policy": "path_scope",
             "writable": [default_cwd] if default_cwd else [],
             "readable": [default_cwd] if default_cwd else []},
        ]

    for entry in entries:
        policy_name = entry.get("policy")
        factory = HOOK_POLICIES.get(policy_name)
        if factory is None:
            raise UnknownPolicyError(
                f"unknown hook policy {policy_name!r}; "
                f"available: {sorted(HOOK_POLICIES)}"
            )
        params = {k: v for k, v in entry.items() if k != "policy"}
        matchers.append(factory(**params))

    return {"PreToolUse": matchers}
