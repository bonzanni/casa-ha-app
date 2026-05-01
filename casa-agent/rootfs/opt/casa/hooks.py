"""Safety hooks: command blocking and parameterized path-scope enforcement.

Per-agent hook wiring is driven by each agent's ``hooks.yaml`` file,
resolved through :func:`resolve_hooks` and the :data:`HOOK_POLICIES`
registry. Payload shape follows the SDK's
``PreToolUseHookSpecificOutput``: ``hookEventName`` +
``permissionDecision`` (allow | deny | ask) + reason.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

# ---------------------------------------------------------------------------
# Forbidden shell commands (argv-aware)
#
# History: pre-v0.14.6 this was a flat list of regex patterns matched against
# the raw command string. That trivially missed equivalents like
# `rm -r -f`, `rm --recursive --force`, `rm -rfv`, etc. The argv-aware
# matcher below splits the command on shell separators (;, &&, ||, |, &),
# shlex'es each piece into argv tokens, and inspects argv[0] + argv[1:].
# It also recurses into `bash -c <str>` / `sh -c <str>` so that wrapper
# shells don't bypass the check.
# FORBIDDEN_PATTERNS is kept as a deprecated alias of the legacy regex list
# so existing imports don't break; the live matcher is _command_is_dangerous.
# ---------------------------------------------------------------------------

# Programs that are denied outright (no allow-listed flag set).
_DENY_PROGRAMS = frozenset({
    "shutdown", "reboot", "halt", "poweroff", "ssh", "scp",
})

# Wrapper shells whose -c argument we should re-scan.
_WRAPPER_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ash", "ksh"})

# Shell-control operators on which we split the command into pipeline pieces.
# Order matters in the regex: longer operators first so we don't double-split.
_PIPELINE_SPLIT_RE = re.compile(r"&&|\|\||;|\||&")

# Env-var assignment prefix (FOO=bar cmd) — skipped when locating argv[0].
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Kept for back-compat with any external code that imported the old name.
# Not used by block_dangerous_commands — the argv matcher is authoritative.
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


def _split_pipeline(command: str) -> list[list[str]]:
    """Split a shell command line into a list of argv lists.

    Splits on ;, &&, ||, |, & (ignoring quoted contents via shlex), then
    runs shlex.split on each piece. Leading FOO=bar assignments are
    stripped so argv[0] is the real program name.
    """
    pieces = _PIPELINE_SPLIT_RE.split(command)
    out: list[list[str]] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        try:
            argv = shlex.split(piece, posix=True)
        except ValueError:
            # Mismatched quotes — fall back to whitespace split. Permissive
            # by intent: a malformed command is the user's problem; we
            # still want to *try* to detect dangerous primitives in it.
            argv = piece.split()
        while argv and _ENV_ASSIGN_RE.match(argv[0]):
            argv = argv[1:]
        if argv:
            out.append(argv)
    return out


def _rm_has_recursive_and_force(argv: list[str]) -> bool:
    """True iff `rm` argv contains both a recursive AND a force flag.

    Recognises -r / -R / --recursive and -f / --force in any order, and
    short-flag clusters (-rf, -fr, -rfv, -rfd, -fRv, ...).
    """
    has_recursive = False
    has_force = False
    for arg in argv[1:]:
        if arg == "--":
            break  # everything after -- is positional
        if arg in ("--recursive",):
            has_recursive = True
        elif arg == "--force":
            has_force = True
        elif arg.startswith("--"):
            continue
        elif arg.startswith("-") and len(arg) > 1:
            for ch in arg[1:]:
                if ch in ("r", "R"):
                    has_recursive = True
                elif ch == "f":
                    has_force = True
    return has_recursive and has_force


def _dd_writes_block_device(argv: list[str]) -> bool:
    """True iff dd has if= or of= argument (block-level read or write)."""
    return any(a.startswith("if=") or a.startswith("of=") for a in argv[1:])


def _curl_sends_data(argv: list[str]) -> bool:
    """True iff curl is doing a write request (-X POST/PUT/DELETE/PATCH or -d/--data*)."""
    args = argv[1:]
    write_methods = {"POST", "PUT", "DELETE", "PATCH"}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--request") and i + 1 < len(args):
            if args[i + 1].upper() in write_methods:
                return True
        if a in ("-d", "--data", "--data-raw", "--data-binary",
                 "--data-urlencode", "-T", "--upload-file"):
            return True
        # Combined short forms: -XPOST, -dfoo
        if a.startswith("-X") and len(a) > 2 and a[2:].upper() in write_methods:
            return True
        if a.startswith("-d") and len(a) > 2 and not a.startswith("-d="):
            return True
        i += 1
    return False


def _command_is_dangerous(command: str, *, _depth: int = 0) -> str | None:
    """Return a human-readable reason if the command is dangerous, else None.

    Recurses up to 3 levels deep into wrapper shells (bash -c, sh -c, ...).
    """
    if _depth > 3:
        return None
    for argv in _split_pipeline(command):
        prog_path = argv[0]
        # Strip absolute path: /usr/bin/rm -> rm.
        prog = os.path.basename(prog_path)

        # Wrapper-shell recursion: bash -c "<cmd>" / sh -c "<cmd>".
        if prog in _WRAPPER_SHELLS:
            for j in range(1, len(argv) - 1):
                if argv[j] == "-c":
                    inner_reason = _command_is_dangerous(
                        argv[j + 1], _depth=_depth + 1,
                    )
                    if inner_reason:
                        return f"{prog} -c wrapping {inner_reason}"
                    break  # don't double-scan the same -c
            # Fall through — the outer wrapper shell itself isn't denied.

        if prog == "rm" and _rm_has_recursive_and_force(argv):
            return f"rm with recursive+force flags: {shlex.join(argv)!r}"
        if prog == "dd" and _dd_writes_block_device(argv):
            return f"dd with if=/of= argument: {shlex.join(argv)!r}"
        if prog == "curl" and _curl_sends_data(argv):
            return f"curl sending data: {shlex.join(argv)!r}"
        if prog in _DENY_PROGRAMS:
            return f"{prog} is not allowed"
    return None


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
    # L-2 (v0.34.2): when the input was an absolute path, parts[0] == "/".
    # Naive "/".join produces "//rest" — return "/rest" instead.
    if resolved and resolved[0] == "/":
        return "/" + "/".join(resolved[1:])
    return "/".join(resolved)


# ---------------------------------------------------------------------------
# Hook implementations
# ---------------------------------------------------------------------------


async def block_dangerous_commands(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """Block Bash commands that contain dangerous primitives.

    Uses an argv-aware matcher (see ``_command_is_dangerous``) so that
    flag variations (``-r -f`` vs ``-rf``, ``--recursive --force``,
    short-flag clusters) and wrapper shells (``bash -c "..."``) all
    resolve to the same decision.
    """
    tool_name = input_data.get("tool_name", "")
    if tool_name != "Bash":
        return None

    command = input_data.get("tool_input", {}).get("command", "")
    reason = _command_is_dangerous(command)
    if reason is not None:
        return _deny(f"Blocked by safety hook: {reason}")
    return None


# ---------------------------------------------------------------------------
# Parameterized path_scope + HOOK_POLICIES registry.
# ---------------------------------------------------------------------------


class UnknownPolicyError(Exception):
    """Raised when a hooks.yaml policy or parameter is not recognised."""


def make_path_scope_hook_v2(
    *,
    writable: list[str] | None = None,
    readable: list[str] | None = None,
) -> HookCallback:
    """Return a PreToolUse hook that enforces absolute-path prefixes.

    The per-agent ``hooks.yaml`` supplies the prefix lists.
    ``writable`` applies to Write/Edit. ``readable`` applies to
    Read/Write/Edit. Anything outside the allowed set denies;
    exact-match or prefix-match.
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


# ---------------------------------------------------------------------------
# casa_config_guard - Plan 3 (blocks /data/, schema/, resident deletions)
# ---------------------------------------------------------------------------


_RESIDENT_RM_RE = re.compile(
    r"\brm\s+(-[a-zA-Z]+\s+)*"
    r"/addon_configs/casa-agent/agents/(?!specialists/|executors/)[^/\s]+"
)


def make_casa_config_guard_hook(
    *,
    forbid_write_paths: list[str] | None = None,
    forbid_delete_residents: bool = True,
) -> HookCallback:
    """Return a PreToolUse hook that guards Casa-specific destructive ops."""
    forbid_write = [_normalize_path(p) for p in (forbid_write_paths or [])]

    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        tool_name = input_data.get("tool_name", "")
        if tool_name in ("Write", "Edit"):
            raw = input_data.get("tool_input", {}).get("file_path", "")
            norm = _normalize_path(raw)
            if _has_prefix(norm, forbid_write):
                return _deny(
                    f"casa_config_guard: {tool_name} blocked - {raw!r} is "
                    f"in a forbidden prefix ({forbid_write}). This path "
                    f"holds runtime state or authoritative schema; editing "
                    f"it would break Casa. Ask the user if you believe "
                    f"this is necessary."
                )
        elif tool_name == "Bash":
            command = input_data.get("tool_input", {}).get("command", "")
            if forbid_delete_residents and _RESIDENT_RM_RE.search(command):
                return _deny(
                    "casa_config_guard: Bash blocked - command looks like "
                    "a resident agent deletion. Residents are very "
                    "destructive to remove; ask the user explicitly in the "
                    "engagement topic and retry only if they say yes."
                )
        return None

    return _hook


# ---------------------------------------------------------------------------
# commit_size_guard - Plan 3 (asks user before batch commits > N files)
# ---------------------------------------------------------------------------


def _git_porcelain_count(repo_dir: str = "/addon_configs/casa-agent") -> int:
    """Return the number of lines in ``git status --porcelain``.

    Isolated for testability - tests monkeypatch this function.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir, capture_output=True, text=True, check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def make_commit_size_guard_hook(*, max_files: int) -> HookCallback:
    """Deny Write/Edit when >= max_files are already uncommitted.

    Forces the agent to emit_completion + config_git_commit before
    piling on more changes.
    """
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        tool_name = input_data.get("tool_name", "")
        if tool_name not in ("Write", "Edit"):
            return None
        count = _git_porcelain_count()
        if count > max_files:
            return _deny(
                f"commit_size_guard: {count} files already uncommitted "
                f"(max={max_files}). Call config_git_commit to stage your "
                f"current batch, then continue. If you must commit more "
                f"than {max_files} files atomically, ask the user first."
            )
        return None

    return _hook


# ---------------------------------------------------------------------------
# HOOK_POLICIES — two-tier registry (Plan 4a.1).
#
# Each entry is {"matcher": regex, "factory": fn(**kwargs) -> HookCallback}.
# The "matcher" regex names the CC tool names the policy applies to; it's
# used identically by both consumers:
#   - SDK path (resolve_hooks below) passes it to HookMatcher(matcher=...).
#   - HTTP path (_build_cc_hook_policies in casa_core.py) gates the
#     HookCallback invocation on the CC tool name before dispatching.
# The "factory" returns a raw async HookCallback — the same coroutine shape
# produced by make_*_hook_* helpers above. Both consumers call it directly.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# self_containment_guard - Plan 4b §6.6 / P-6
# (pre-push grep for §2.0 self-containment anti-patterns)
# ---------------------------------------------------------------------------

_PLEASE_INSTALL_RE = re.compile(
    r"(please\s+install|manually\s+install|fork\s+the\s+dockerfile)",
    re.IGNORECASE,
)
_APT_CMD_RE = re.compile(r"\b(apt|apt-get|yum|dnf|pacman)\s+install\b")
_NONBASELINE_BIN_RE = re.compile(
    r"/usr/(local/)?bin/(terraform|kubectl|aws|ffmpeg|helm|docker|packer|ansible)\b"
)


def make_self_containment_guard() -> HookCallback:
    """Pre-push grep for §2.0 self-containment anti-patterns."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        if input_data.get("tool_name") != "Bash":
            return None
        cmd = input_data.get("tool_input", {}).get("command", "")
        if not re.match(r"\s*git\s+push\b", cmd):
            return None

        cwd = Path(input_data.get("cwd") or os.getcwd())
        if not cwd.is_dir():
            return None

        findings: list[str] = []
        for root, dirs, files in os.walk(cwd):
            # Skip VCS + deps.
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
            for f in files:
                p = Path(root) / f
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if f.lower() == "readme.md" and _PLEASE_INSTALL_RE.search(content):
                    findings.append(f"{p.relative_to(cwd)}: 'please install X manually'")
                if f.endswith((".sh", ".bash")) and _APT_CMD_RE.search(content):
                    findings.append(f"{p.relative_to(cwd)}: apt/yum install")
                if f.endswith((".py", ".js", ".ts", ".sh")) and _NONBASELINE_BIN_RE.search(content):
                    findings.append(f"{p.relative_to(cwd)}: hardcoded non-baseline binary path")

        if findings:
            return _deny(
                "Blocked by self_containment_guard (§2.0 axiom):\n"
                + "\n".join(f"- {fi}" for fi in findings)
                + "\nDeclare via casa.systemRequirements or use ${CLAUDE_PLUGIN_ROOT}. "
                "Override with --allow-anti-pattern only if false positive."
            )
        return None

    return hook


def _self_containment_guard_factory(**kwargs: Any) -> HookCallback:
    if kwargs:
        raise UnknownPolicyError(
            f"self_containment_guard takes no parameters; got {list(kwargs)}"
        )
    return make_self_containment_guard()


def _block_dangerous_bash_factory(**kwargs: Any) -> HookCallback:
    if kwargs:
        raise UnknownPolicyError(
            f"block_dangerous_bash takes no parameters; got {list(kwargs)}"
        )
    return block_dangerous_commands


def _path_scope_factory(**kwargs: Any) -> HookCallback:
    writable = kwargs.pop("writable", None)
    readable = kwargs.pop("readable", None)
    if kwargs:
        raise UnknownPolicyError(
            f"path_scope: unknown parameter(s) {list(kwargs)}; "
            f"supported: writable, readable"
        )
    return make_path_scope_hook_v2(writable=writable, readable=readable)


def _casa_config_guard_factory(**kwargs: Any) -> HookCallback:
    forbid_write_paths = kwargs.pop("forbid_write_paths", None)
    forbid_delete_residents = kwargs.pop("forbid_delete_residents", True)
    if kwargs:
        raise UnknownPolicyError(
            f"casa_config_guard: unknown parameter(s) {list(kwargs)}; "
            f"supported: forbid_write_paths, forbid_delete_residents"
        )
    return make_casa_config_guard_hook(
        forbid_write_paths=forbid_write_paths,
        forbid_delete_residents=forbid_delete_residents,
    )


def _commit_size_guard_factory(**kwargs: Any) -> HookCallback:
    max_files = int(kwargs.pop("max_files", 20))
    if kwargs:
        raise UnknownPolicyError(
            f"commit_size_guard: unknown parameter(s) {list(kwargs)}; "
            f"supported: max_files"
        )
    return make_commit_size_guard_hook(max_files=max_files)


HOOK_POLICIES: dict[str, dict[str, Any]] = {
    "block_dangerous_bash": {
        "matcher": "Bash",
        "factory": _block_dangerous_bash_factory,
    },
    "path_scope": {
        "matcher": "Read|Write|Edit",
        "factory": _path_scope_factory,
    },
    "casa_config_guard": {
        "matcher": "Write|Edit|Bash",
        "factory": _casa_config_guard_factory,
    },
    "commit_size_guard": {
        "matcher": "Write|Edit",
        "factory": _commit_size_guard_factory,
    },
    "self_containment_guard": {
        "matcher": "Bash",
        "factory": _self_containment_guard_factory,
    },
}


def resolve_hooks(
    config: "HooksConfig",
    *,
    default_cwd: str,
) -> dict[str, list[Any]]:
    """Turn a HooksConfig into ``{"PreToolUse": [HookMatcher, ...]}``.

    Builds SDK HookMatcher objects from the two-tier HOOK_POLICIES shape.
    The factory returns a raw HookCallback; HookMatcher wraps it with the
    policy's matcher regex.
    """
    from claude_agent_sdk import HookMatcher

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
        policy = HOOK_POLICIES.get(policy_name)
        if policy is None:
            raise UnknownPolicyError(
                f"unknown hook policy {policy_name!r}; "
                f"available: {sorted(HOOK_POLICIES)}"
            )
        params = {k: v for k, v in entry.items() if k != "policy"}
        callback = policy["factory"](**params)
        matchers.append(HookMatcher(
            matcher=policy["matcher"],
            hooks=[callback],
        ))

    return {"PreToolUse": matchers}
