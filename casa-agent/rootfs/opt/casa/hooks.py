"""Safety hooks: command blocking and parameterized path-scope enforcement.

Per-agent hook wiring is driven by each agent's ``hooks.yaml`` file,
resolved through :func:`resolve_hooks` and the :data:`HOOK_POLICIES`
registry. Payload shape follows the SDK's
``PreToolUseHookSpecificOutput``: ``hookEventName`` +
``permissionDecision`` (allow | deny | ask) + reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

from cc_tool_pattern import matches_any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Forbidden shell commands (argv-aware)
#
# History: pre-v0.14.6 this was a flat list of regex patterns matched against
# the raw command string. That trivially missed equivalents like
# `rm -r -f`, `rm --recursive --force`, `rm -rfv`, etc. The argv-aware
# matcher below splits the command on shell separators (;, &&, ||, |, &,
# and newlines), shlex'es each piece into argv tokens, and inspects
# argv[0] + argv[1:]. It also recurses into `bash -c <str>` / `sh -c <str>`
# so that wrapper shells don't bypass the check.
# FORBIDDEN_PATTERNS is kept as a deprecated alias of the legacy regex list
# so existing imports don't break; the live matcher is _command_is_dangerous.
#
# SCOPE & ACCEPTED RESIDUALS (v0.50.0 security review):
# block_dangerous_commands and casa_config_guard are DEFENSE-IN-DEPTH argv
# inspectors, not a bash sandbox — the real security boundaries are the SDK
# permission system and workspace isolation. Known residuals, inherent to
# argv-level inspection, accepted and documented rather than chased with
# ever-more regex:
#   * command substitution ``$(...)`` / backticks and process substitution
#     ``<(...)`` / ``>(...)``: scanned best-effort by span regexes (see
#     _SUBSTITUTION_SPAN_RE below); adversarial quoting/nesting inside a
#     span can still evade.
#   * destructive verbs outside the modeled set: ``find -delete`` /
#     ``find ... -exec rm ...``, ``truncate``, ``shred``, ``tee /target``,
#     ``> file`` clobbering, non-``rm`` deletion of residents, etc. — a
#     deletion hidden behind a verb we do not model is not decomposed.
#   * anything requiring evaluation of attacker-controlled data: variable
#     indirection (``X='rm -rf /'; $X``), ``env -S 'rm ...'``, paths built
#     from shell variables (``rm${IFS}-rf${IFS}/``), xargs arguments
#     arriving via stdin.
#   * exec-wrapper prefixes outside the modeled set: the shell builtins
#     ``command``/``exec`` (builtins, not external programs, so absent from
#     _EXEC_WRAPPER_ARG_FLAGS), and wrapper nests deeper than the recursion
#     bound (``_depth > 3`` — e.g. four stacked wrappers like
#     ``sudo nohup setsid timeout 5 rm -rf /``): the innermost command is
#     not unwrapped/re-scanned.
# Do NOT present these guards as complete command filtering; strengthen the
# outer boundaries instead. Honesty over false assurance.
# ---------------------------------------------------------------------------

# Programs that are denied outright (no allow-listed flag set).
_DENY_PROGRAMS = frozenset({
    "shutdown", "reboot", "halt", "poweroff", "ssh", "scp",
})

# Wrapper shells whose -c argument we should re-scan.
_WRAPPER_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ash", "ksh"})

# Shell-control operators on which we split the command into pipeline pieces.
# Order matters in the regex: longer operators first so we don't double-split.
# ``[\r\n]+`` makes newlines (LF/CRLF) first-class command separators — an
# LLM-issued multi-line command is exactly as dangerous as its ``;``-joined
# equivalent. Only used by the legacy quote-blind fallback below.
_PIPELINE_SPLIT_RE = re.compile(r"&&|\|\||;|\||&|[\r\n]+")

# Pipeline separators as *tokens* (for the quote-aware shlex splitter).
# v0.50.0 round 2: bash's '|&' (pipe stdout+stderr) and the case-branch
# terminators ';&' / ';;&' each begin a new simple command, and shlex's
# punctuation-run tokenizer emits each as ONE token — absent from this set
# they fell through to the _SHLEX_PUNCT skip below and the two stages
# merged, so the RHS program was never scanned as argv[0].
# True redirections ('>', '>>', '<', '>&', '&>', and the '>&' inside
# '2>&1') must stay OUT of this set: they do not start a new command, and
# splitting on them would promote a redirection target to argv[0].
_PIPELINE_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", ";&", ";;&"})

# shlex ``punctuation_chars=True`` emits these as standalone operator tokens.
# Non-pipeline operators (redirections ``>`` ``<`` ``>>``, subshell ``(`` ``)``)
# are skipped so a redirection target isn't promoted to argv[0] of a new piece.
_SHLEX_PUNCT = frozenset("();<>|&")

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


def _split_pipeline_fallback(command: str) -> list[list[str]]:
    """Legacy quote-blind split; used only when the whole command has
    mismatched quotes and the quote-aware tokenizer cannot parse it.

    Splits on the raw operator regex, then runs shlex.split on each piece
    with a whitespace-split fallback. Leading FOO=bar assignments are
    stripped so argv[0] is the real program name. Permissive by intent: a
    malformed command is the user's problem; we still want to *try* to
    detect dangerous primitives in it.
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
            argv = piece.split()
        while argv and _ENV_ASSIGN_RE.match(argv[0]):
            argv = argv[1:]
        if argv:
            out.append(argv)
    return out


def _split_pipeline(command: str) -> list[list[str]]:
    """Split a shell command line into a list of argv lists, quote-aware.

    Splits on ;, &&, ||, |, & AND newlines. Uses shlex with
    ``punctuation_chars`` so those operators are NOT treated as pipeline
    boundaries when they appear inside quoted strings (e.g.
    ``git commit -m "a && b"`` stays one argv). Leading FOO=bar
    assignments are stripped so argv[0] is the real program name.
    Falls back to the legacy quote-blind split on mismatched quotes.
    """
    # Shell line-continuation: backslash-newline joins physical lines into
    # one logical command. Collapse it BEFORE newline splitting so
    # ``curl \<newline> -X POST ...`` stays a single argv.
    command = command.replace("\\\r\n", " ").replace("\\\n", " ")
    # Newlines are command separators equivalent to ';'. Rewriting them to
    # ' ; ' is quote-safe: a newline INSIDE quotes becomes a ';' that the
    # tokenizer keeps as literal data (the surrounding quotes are preserved),
    # while a bare newline becomes a real separator token.
    command = re.sub(r"[\r\n]+", " ; ", command)
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        # Disable '#' comment handling (shlex.shlex defaults commenters='#',
        # unlike shlex.split(comments=False)). Otherwise a '#' would swallow
        # the rest of the (newline-collapsed) command — e.g.
        # ``ls # note\nrm -rf /`` would hide the rm and bypass the guard.
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        # Mismatched quotes — permissive best-effort, same as before.
        return _split_pipeline_fallback(command)
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _PIPELINE_SEPARATORS:
            if cur:
                out.append(cur)
                cur = []
        elif tok and all(ch in _SHLEX_PUNCT for ch in tok):
            # Non-pipeline operator token (>, <, >>, (, ) ...): skip it so
            # a redirection target stays inside the current argv instead of
            # being promoted to argv[0] of a new piece.
            continue
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    result: list[list[str]] = []
    for argv in out:
        while argv and _ENV_ASSIGN_RE.match(argv[0]):
            argv = argv[1:]
        if argv:
            result.append(argv)
    return result


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


# v0.50.0 (security-review follow-up, H8 class): command/process
# substitution and backticks execute their contents without the inner
# program ever appearing as argv[0] of a pipeline piece, so the argv
# scanner alone never saw `echo $(rm -rf /)` or ``x=`rm -rf /` ``.
# The spans are extracted from the RAW command string (quote-blind by
# design: `"$(...)"` still executes, and after shlex de-quoting single
# and double quotes are indistinguishable — deny both). Only fires when
# the *inner* content is itself dangerous, so `awk '{print $(NF-1)}'`
# and `echo $((1+2))` stay allowed.
_SUBSTITUTION_SPAN_RE = re.compile(r"[$<>]\((.*)\)", re.DOTALL)
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")

# xargs flags that consume the FOLLOWING token as their argument (GNU
# xargs flags whose argument must be separate or may be separate).
# Attached forms (-n1, -I{}, --max-args=1) are skipped by the generic
# leading-dash test. -e/-i/-l take only attached optional args in GNU
# xargs, so they are deliberately NOT in this set — consuming the next
# token for them could swallow the wrapped command (a false negative).
_XARGS_ARG_FLAGS = frozenset({
    "-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s",
    "--arg-file", "--delimiter", "--eof", "--max-lines", "--max-args",
    "--max-procs", "--max-chars",
})


def _xargs_wrapped_argv(argv: list[str]) -> list[str]:
    """Return the argv of the command ``xargs`` will exec, or ``[]``.

    Skips xargs' own flags (consuming separate arguments where the flag
    requires one); the first non-flag token starts the wrapped command.
    """
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _XARGS_ARG_FLAGS:
            i += 2
            continue
        if a.startswith("-") and a != "-":
            i += 1
            continue
        return argv[i:]
    return []


# v0.50.0 round 2: exec-wrapper prefixes. Each of these programs runs its
# tail as the real command (``nohup rm -rf /``, ``timeout 5 rm -rf /``,
# ``sudo rm ...``) — pre-fix argv[0] was the benign wrapper name and the
# tail was never inspected. Map: wrapper name -> its flags that consume the
# FOLLOWING token as a separate argument (attached forms like ``-n5`` /
# ``--adjustment=5`` are skipped by the generic leading-dash test — same
# convention as _XARGS_ARG_FLAGS above).
_EXEC_WRAPPER_ARG_FLAGS: dict[str, frozenset[str]] = {
    "nohup": frozenset(),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "setsid": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata",
                         "-p", "--pid", "-P", "--pgid", "-u", "--uid"}),
    "chrt": frozenset({"-T", "--sched-runtime", "-P", "--sched-period",
                       "-D", "--sched-deadline", "-p", "--pid"}),
    "taskset": frozenset(),
    "unbuffer": frozenset(),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-h", "--host",
                       "-p", "--prompt", "-C", "--close-from",
                       "-D", "--chdir", "-R", "--chroot",
                       "-T", "--command-timeout", "-U", "--other-user",
                       "-r", "--role", "-t", "--type"}),
    "doas": frozenset({"-u", "-C", "-a"}),
}

# Wrappers with a MANDATORY positional before the command: ``timeout
# DURATION cmd``, ``chrt PRIORITY cmd``, ``taskset MASK cmd``. That many
# non-flag tokens are skipped before the tail starts.
_EXEC_WRAPPER_POSITIONALS: dict[str, int] = {
    "timeout": 1, "chrt": 1, "taskset": 1,
}


def _exec_wrapper_tail(argv: list[str]) -> list[str]:
    """Return the argv of the command an exec-wrapper prefix will run, or ``[]``.

    Same pattern as :func:`_xargs_wrapped_argv`: skip the wrapper name, its
    option flags (consuming the following token where the flag takes a
    separate argument), ``NAME=VALUE`` assignments (``env A=B cmd``,
    ``sudo VAR=x cmd``), and any mandatory positional (timeout's DURATION,
    chrt's PRIORITY, taskset's MASK). The first remaining token starts the
    wrapped command. Conservative by design: when a flag's arity is not in
    the table, the tail starts at the first non-flag/non-assignment token.
    """
    prog = os.path.basename(argv[0])
    arg_flags = _EXEC_WRAPPER_ARG_FLAGS.get(prog)
    if arg_flags is None:
        return []
    skip_positionals = _EXEC_WRAPPER_POSITIONALS.get(prog, 0)
    i = 1
    opts_done = False
    while i < len(argv):
        a = argv[i]
        if not opts_done and a == "--":
            opts_done = True
            i += 1
            continue
        if not opts_done and a in arg_flags:
            i += 2
            continue
        if not opts_done and a.startswith("-") and a != "-":
            i += 1
            continue
        if _ENV_ASSIGN_RE.match(a):
            i += 1
            continue
        if skip_positionals > 0:
            skip_positionals -= 1
            i += 1
            continue
        return argv[i:]
    return []


def _argv_is_dangerous(argv: list[str], *, _depth: int = 0) -> str | None:
    """Inspect a single pipeline piece (argv list) for dangerous primitives."""
    if _depth > 3 or not argv:
        return None
    # Strip absolute path: /usr/bin/rm -> rm.
    prog = os.path.basename(argv[0])

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

    # eval concatenates its arguments and executes them as a shell command.
    if prog == "eval" and len(argv) > 1:
        inner_reason = _command_is_dangerous(
            " ".join(argv[1:]), _depth=_depth + 1,
        )
        if inner_reason:
            return f"eval wrapping {inner_reason}"

    # xargs execs its trailing argv (with stdin-derived arguments appended).
    if prog == "xargs":
        wrapped = _xargs_wrapped_argv(argv)
        if wrapped:
            inner_reason = _argv_is_dangerous(wrapped, _depth=_depth + 1)
            if inner_reason:
                return f"xargs wrapping {inner_reason}"

    # v0.50.0 round 2: exec-wrapper prefixes (nohup, timeout, env, sudo, ...)
    # run their tail as the real command — unwrap and re-scan it as its own
    # argv, so `timeout 5 rm -rf /` resolves like `rm -rf /`.
    if prog in _EXEC_WRAPPER_ARG_FLAGS:
        wrapped = _exec_wrapper_tail(argv)
        if wrapped:
            inner_reason = _argv_is_dangerous(wrapped, _depth=_depth + 1)
            if inner_reason:
                return f"{prog} wrapping {inner_reason}"

    if prog == "rm" and _rm_has_recursive_and_force(argv):
        return f"rm with recursive+force flags: {shlex.join(argv)!r}"
    if prog == "dd" and _dd_writes_block_device(argv):
        return f"dd with if=/of= argument: {shlex.join(argv)!r}"
    if prog == "curl" and _curl_sends_data(argv):
        return f"curl sending data: {shlex.join(argv)!r}"
    if prog in _DENY_PROGRAMS:
        return f"{prog} is not allowed"
    return None


def _command_is_dangerous(command: str, *, _depth: int = 0) -> str | None:
    """Return a human-readable reason if the command is dangerous, else None.

    Recurses up to 3 levels deep into wrapper shells (bash -c, sh -c, ...),
    ``eval``, ``xargs``, and command/process-substitution spans.
    """
    if _depth > 3:
        return None
    for argv in _split_pipeline(command):
        reason = _argv_is_dangerous(argv, _depth=_depth)
        if reason:
            return reason
    # Command substitution $(...), process substitution <(...)/>(...), and
    # backticks all execute their contents; scan those spans recursively.
    for span_re in (_SUBSTITUTION_SPAN_RE, _BACKTICK_SPAN_RE):
        for m in span_re.finditer(command):
            inner_reason = _command_is_dangerous(
                m.group(1), _depth=_depth + 1,
            )
            if inner_reason:
                return f"substitution wrapping {inner_reason}"
    return None


HookCallback = Callable[
    [dict[str, Any], str | None, dict[str, Any]],
    Awaitable[dict[str, Any]],
]
# H-2 (v0.36.1): no-op paths return ``{}`` not ``None``. The SDK's
# ``_convert_hook_output_for_cli`` calls ``hook_output.items()``
# unconditionally; ``None`` violates the typed ``HookJSONOutput`` contract
# and emits 73+ ``Error in hook callback`` lines per ~30-min engagement.
# Operationally equivalent (SDK treats ``{}`` the same as ``None`` for
# decision purposes) but type-compliant.


def _active_claude_code_driver() -> Any:
    """Resolve the live ``claude_code`` driver for the §2 F1(a) permission-
    keyboard discrete-send seam. Returns ``None`` (⇒ eager fallback) when no
    driver is attached (unit tests / degraded boot)."""
    try:
        import agent as _agent_mod
        return getattr(_agent_mod, "active_claude_code_driver", None)
    except Exception:  # noqa: BLE001
        return None


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
    # v0.50.0 (security-review must-fix): collapse redundant slashes FIRST.
    # POSIX leaves a leading '//' implementation-defined and PurePosixPath
    # preserves it as a distinct root ('//config' -> parts ('//', 'config')),
    # so '//config/agents/x' normalized to the malformed
    # '///config/agents/x' and slipped past every prefix check — while the
    # Linux kernel resolves '//' as '/', making the command effective.
    raw = re.sub(r"/{2,}", "/", raw)
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
) -> dict[str, Any]:
    """Block Bash commands that contain dangerous primitives.

    Uses an argv-aware matcher (see ``_command_is_dangerous``) so that
    flag variations (``-r -f`` vs ``-rf``, ``--recursive --force``,
    short-flag clusters) and wrapper shells (``bash -c "..."``) all
    resolve to the same decision.
    """
    tool_name = input_data.get("tool_name", "")
    if tool_name != "Bash":
        return {}

    command = input_data.get("tool_input", {}).get("command", "")
    reason = _command_is_dangerous(command)
    if reason is not None:
        return _deny(f"Blocked by safety hook: {reason}")
    # Sol #5: also deny a Bash write into /config/plugins (registry/store).
    # path_scope ignores Bash, so a claude_code executor (plugin-developer,
    # configurator) could otherwise `echo > /config/plugins/registry.json`,
    # bypassing plugin_add validation + §3.9 sequencing. Both executors carry
    # block_dangerous_bash, so this closes the HTTP-hook path too — the same
    # regex the resident/specialist settings guard uses.
    if _PLUGINS_WRITE_RE.search(command):
        return _deny(_PLUGINS_DENY_MSG)
    return {}


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
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name not in ("Read", "Write", "Edit"):
            return {}
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
        return {}

    return _hook


def _has_prefix(norm: str, prefixes: list[str]) -> bool:
    return any(norm == p or norm.startswith(p.rstrip("/") + "/")
               for p in prefixes)


# ---------------------------------------------------------------------------
# casa_config_guard - Plan 3 (blocks /data/, schema/, resident deletions)
# ---------------------------------------------------------------------------


_RESIDENT_ROOT = "/config/agents"
_RESIDENT_EXEMPT_SUBTREES = ("specialists", "executors")


def _argv_deletes_resident(argv: list[str], *, _depth: int = 0) -> bool:
    """One pipeline piece of :func:`_deletes_resident` (argv level)."""
    if _depth > 3 or not argv:
        return False
    prog = os.path.basename(argv[0])
    if prog in _WRAPPER_SHELLS:
        for j in range(1, len(argv) - 1):
            if argv[j] == "-c":
                if _deletes_resident(argv[j + 1], _depth=_depth + 1):
                    return True
                break
    # v0.50.0: eval concatenates its arguments and executes them as a
    # shell command — same wrapper class as bash -c.
    if prog == "eval" and len(argv) > 1:
        if _deletes_resident(" ".join(argv[1:]), _depth=_depth + 1):
            return True
    # v0.50.0 round 2: exec-wrapper prefixes (nohup, timeout, env, sudo,
    # ...) run their tail as the real command — unwrap and re-scan, same
    # as in _argv_is_dangerous.
    if prog in _EXEC_WRAPPER_ARG_FLAGS:
        wrapped = _exec_wrapper_tail(argv)
        if wrapped and _argv_deletes_resident(wrapped, _depth=_depth + 1):
            return True
    if prog != "rm":
        return False
    seen_ddash = False
    for a in argv[1:]:
        if not seen_ddash:
            if a == "--":
                seen_ddash = True
                continue
            if a.startswith("-") and a != "-":
                continue  # short or long flag
        norm = _normalize_path(a)
        if norm == _RESIDENT_ROOT:
            return True  # rm of the whole agents dir kills every resident
        if norm.startswith(_RESIDENT_ROOT + "/"):
            rest = norm[len(_RESIDENT_ROOT) + 1:]
            head, _sep, tail = rest.partition("/")
            # Exempt only paths INSIDE specialists/ or executors/;
            # deleting those subtree roots themselves still denies
            # (matches the old regex's lookahead semantics).
            if head in _RESIDENT_EXEMPT_SUBTREES and tail:
                continue
            return True
    return False


def _deletes_resident(command: str, *, _depth: int = 0) -> bool:
    """True iff any ``rm`` in the command (or behind a ``bash``/``sh -c``
    wrapper, ``eval``, or an exec-wrapper prefix like ``nohup``/``timeout``/
    ``sudo``) targets ``/config/agents`` itself or a
    non-specialist/non-executor child of it.

    Argv-aware (via :func:`_split_pipeline`), so it is immune to the
    bypasses that defeated the old regex: quoted paths
    (``rm -r "/config/agents/ellen"``), long flags (``--recursive``), the
    ``--`` end-of-options marker, wrapper shells, exec-wrapper prefixes,
    and ``..`` traversal (paths are normalised with
    :func:`_normalize_path`).
    """
    if _depth > 3:
        return False
    for argv in _split_pipeline(command):
        if _argv_deletes_resident(argv, _depth=_depth):
            return True
    return False


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
    ) -> dict[str, Any]:
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
            if forbid_delete_residents and _deletes_resident(command):
                return _deny(
                    "casa_config_guard: Bash blocked - command looks like "
                    "a resident agent deletion. Residents are very "
                    "destructive to remove; ask the user explicitly in the "
                    "engagement topic and retry only if they say yes."
                )
        return {}

    return _hook


# ---------------------------------------------------------------------------
# I-2 (v0.69.8): agent-home settings.json self-grant guard
# ---------------------------------------------------------------------------


_SETTINGS_JSON_SUFFIX = "/.claude/settings.json"

_SETTINGS_DENY_MSG = (
    "settings_guard: editing .claude/settings.json is not permitted — plugin "
    "grants are configurator-managed. Ask the configurator to install/enable a "
    "plugin instead of editing settings.json directly."
)

# A Bash command with a write operator (redirect / tee / dd / cp / mv /
# install / sed -i / truncate) appearing BEFORE a .claude/settings.json path —
# i.e. writing INTO settings.json. A bare `cat …/settings.json` (read) has no
# preceding write op and is allowed. Best-effort — see the Bash branch of
# make_agent_home_settings_guard.
_SETTINGS_JSON_WRITE_RE = re.compile(
    r"(?:>>?|\btee\b|\bdd\b|\bcp\b|\bmv\b|\binstall\b|sed\s+-i|\btruncate\b)"
    r".*?\.claude/settings\.json",
    re.IGNORECASE | re.DOTALL,
)

# Unified plugin architecture (§3.11/§3.13): /config/plugins/ (registry.json +
# the content-addressed store + staging) is the single plugin-assignment
# authority. An engagement with Write/Edit/Bash could self-assign plugins by
# editing the registry directly, bypassing plugin_add's validation + §3.9
# sequencing. Deny direct writes under it (same residual as I-2: an obfuscated
# `permission_mode: auto` Bash command can still slip through — the complete
# boundary is sandbox enforcement).
_PLUGINS_DIR_PREFIX = "/config/plugins/"
_PLUGINS_DENY_MSG = (
    "Direct writes under /config/plugins/ are refused. The plugin registry and "
    "store are the single assignment authority — mutate them via the "
    "configurator's plugin_add / plugin_update / plugin_assign / "
    "plugin_unassign / plugin_remove tools, never by hand."
)
_PLUGINS_WRITE_RE = re.compile(
    # A write-ish verb followed (anywhere) by the plugins path. Sol round-3 B3a:
    # broadened with chmod/chown/ln/touch (mode/link tamper) + a trailing-slash-
    # or word-boundary match so exact `/config/plugins` is covered too.
    r"(?:>>?|\btee\b|\bdd\b|\bcp\b|\bmv\b|\bln\b|\binstall\b|sed\s+-i|"
    r"\btruncate\b|\brm\b|\bmkdir\b|\bchmod\b|\bchown\b|\btouch\b|\bcd\b)"
    r".*?/config/plugins(?:/|\b)",
    re.IGNORECASE | re.DOTALL,
)
# Language-runtime write to the path (python/node/perl `open(...,'w')`, etc.).
# Targeted at WRITE modes so a plain READ of /config/plugins/store (the
# plugin-developer's legitimate access) is not denied. Best-effort — a
# determined obfuscated command still needs the filesystem/privilege boundary
# (spec integrity = content-addressing + checksum DETECTION; tracked backlog).
_PLUGINS_CODE_WRITE_RE = re.compile(
    r"/config/plugins\b[^\n]{0,80}?['\"][wax]\+?b?['\"]"
    r"|['\"][wax]\+?b?['\"][^\n]{0,80}?/config/plugins\b",
    re.IGNORECASE | re.DOTALL,
)


def make_agent_home_settings_guard() -> HookCallback:
    """Deny hand-edits to any ``.claude/settings.json`` (I-2) OR anything under
    ``/config/plugins/`` (unified plugin architecture §3.11/§3.13).

    settings.json is configurator-managed; no agent should hand-edit it. The
    plugin registry + store (``/config/plugins/``) is the single plugin-
    assignment authority (§3.13); a resident/executor with Write/Edit/Bash
    could otherwise self-assign a plugin by editing the registry directly,
    bypassing plugin_add's validation + §3.9 sequencing. Both guards match by
    normalized path, so ``..`` traversal can't slip a write through (see
    `_normalize_path`)."""
    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            ti = input_data.get("tool_input", {})
            raw = ti.get("file_path") or ti.get("notebook_path") or ""
            norm = _normalize_path(raw)
            if norm.endswith(_SETTINGS_JSON_SUFFIX):
                return _deny(_SETTINGS_DENY_MSG)
            # Sol round-3 B3a: cover the exact dir too (not just the trailing-
            # slash prefix).
            if norm.startswith(_PLUGINS_DIR_PREFIX) or norm == "/config/plugins":
                return _deny(_PLUGINS_DENY_MSG)
        elif tool_name == "Bash":
            # Finding 1 (codex review v0.69.10): residents with Bash (Ellen)
            # could bypass the file-tool guard with `echo … >
            # .claude/settings.json`. Deny a Bash command that names a
            # settings.json path AND looks like a write. This is best-effort
            # (a determined obfuscated command can still slip through — the
            # complete boundary is filesystem/sandbox enforcement or removing
            # residents' broad Bash; tracked in ROADMAP-backlog), but it
            # catches the realistic prompt-injection form (a plain redirect).
            command = input_data.get("tool_input", {}).get("command", "")
            if _SETTINGS_JSON_WRITE_RE.search(command):
                return _deny(_SETTINGS_DENY_MSG)
            if (_PLUGINS_WRITE_RE.search(command)
                    or _PLUGINS_CODE_WRITE_RE.search(command)):
                return _deny(_PLUGINS_DENY_MSG)
        return {}

    return _hook


def agent_home_settings_guard_matcher():
    """A ``HookMatcher`` wrapping :func:`make_agent_home_settings_guard`,
    injected code-side into every resident's PreToolUse hooks (I-2, v0.69.8)
    so the self-grant guard is an always-on invariant, not config-removable."""
    from claude_agent_sdk import HookMatcher
    return HookMatcher(
        # Sol round-3 B3a: include NotebookEdit — the hook body already handles
        # it, but the matcher must route it or NotebookEdit writes bypass entirely.
        matcher="Write|Edit|MultiEdit|NotebookEdit|Bash",
        hooks=[make_agent_home_settings_guard()],
    )


# ---------------------------------------------------------------------------
# commit_size_guard - Plan 3 (asks user before batch commits > N files)
# ---------------------------------------------------------------------------


def _git_porcelain_count(repo_dir: str = "/config") -> int:
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
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name not in ("Write", "Edit"):
            return {}
        # M17: `git status --porcelain` is a blocking subprocess (up to 5s).
        # Offload it so a Write/Edit on any agent doesn't freeze the shared
        # event loop. _git_porcelain_count stays a sync module-level function
        # so existing patch("hooks._git_porcelain_count", ...) tests still work.
        count = await asyncio.to_thread(_git_porcelain_count)
        if count > max_files:
            return _deny(
                f"commit_size_guard: {count} files already uncommitted "
                f"(max={max_files}). Call config_git_commit to stage your "
                f"current batch, then continue. If you must commit more "
                f"than {max_files} files atomically, ask the user first."
            )
        return {}

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

# M28: anti-patterns live in small text files; cap the per-file read so a
# multi-hundred-MB asset (or the read of a file that matches no check) can't
# blow up memory or the scan time.
_MAX_SCAN_BYTES = 262_144  # 256 KiB


def _scan_tree_for_anti_patterns(cwd: Path) -> list[str]:
    """Synchronous §2.0 anti-pattern tree scan — run via asyncio.to_thread.

    Filters by filename BEFORE opening a file, so files that can match no
    check (e.g. binaries, images) are never read. Reads are capped at
    ``_MAX_SCAN_BYTES``; a pattern placed beyond that offset is not flagged
    (an accepted tradeoff — anti-patterns live near the top of small files).
    """
    findings: list[str] = []
    for root, dirs, files in os.walk(cwd):
        # Skip VCS + deps.
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for f in files:
            check_readme = f.lower() == "readme.md"
            check_apt = f.endswith((".sh", ".bash"))
            check_bin = f.endswith((".py", ".js", ".ts", ".sh"))
            if not (check_readme or check_apt or check_bin):
                continue  # filename can match no check — do not read it
            p = Path(root) / f
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read(_MAX_SCAN_BYTES)
            except OSError:
                continue
            if check_readme and _PLEASE_INSTALL_RE.search(content):
                findings.append(f"{p.relative_to(cwd)}: 'please install X manually'")
            if check_apt and _APT_CMD_RE.search(content):
                findings.append(f"{p.relative_to(cwd)}: apt/yum install")
            if check_bin and _NONBASELINE_BIN_RE.search(content):
                findings.append(f"{p.relative_to(cwd)}: hardcoded non-baseline binary path")
    return findings


_PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"


def _git_lines(cwd: Path, *args: str) -> list[str] | None:
    """Run git in ``cwd``; stdout lines on success, None on any failure."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.splitlines()


def _scan_mcp_launch_refs(cwd: Path) -> list[str]:
    """P2 (2026-07-18 plan, Sol r4 hardened): every
    ``${CLAUDE_PLUGIN_ROOT}/<rel>`` reference in an ``.mcp.json``
    ``command``/``args``/``env`` (incl. ``--opt=`` and ``:``-joined values —
    the vendored PYTHONPATH pattern) must exist in the **HEAD tree** — the
    commit being pushed. Both the ``.mcp.json`` files themselves AND their
    referenced paths are read from HEAD (Sol r5-2: a worktree edit or
    deletion must neither hide a broken committed file nor flag an
    uncommitted one). ``git ls-tree HEAD`` is the oracle (Sol r4-4: the
    index would bless a staged-but-uncommitted file into a broken pushed
    commit); a working-tree existence test is doubly insufficient (the
    gmail-v0.2.0 ``server/.venv`` existed locally, gitignored). Rejects
    ``..``-escapes and absolute-after-interpolation. Outside a git worktree
    the scan is skipped (a real push would fail there anyway); INSIDE a
    worktree a git failure on the trackedness probe fails CLOSED as a
    finding."""
    from plugin_store import parse_mcp_servers_text

    top = _git_lines(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return []
    root = Path(top[0])
    findings: list[str] = []

    def _candidates(ref: str) -> list[str]:
        return [chunk for chunk in re.split(r"[=:]", ref)
                if chunk.startswith(_PLUGIN_ROOT_VAR + "/")]

    # Sol r5-2: enumerate AND read every .mcp.json from the HEAD tree — the
    # commit being pushed. A worktree edit/deletion must neither hide a
    # broken committed file nor flag an uncommitted one.
    all_head = _git_lines(root, "ls-tree", "-r", "--name-only", "HEAD")
    if all_head is None:
        return ["cannot enumerate the pushed commit (git error) — "
                "failing closed"]
    for rel_mcp in [f for f in all_head
                    if PurePosixPath(f).name == ".mcp.json"]:
        content = _git_lines(root, "show", f"HEAD:{rel_mcp}")
        if content is None:
            findings.append(f"{rel_mcp}: cannot read from the pushed commit "
                            "(git error) — failing closed")
            continue
        servers, _malformed = parse_mcp_servers_text(
            "\n".join(content), source=f"HEAD:{rel_mcp}")
        mcp_dir = str(PurePosixPath(rel_mcp).parent)
        for server, cfg in servers.items():
            args = cfg.get("args")
            env = cfg.get("env")
            refs = ([cfg.get("command")]
                    + list(args if isinstance(args, list) else [])
                    + [v for v in (env.values() if isinstance(env, dict)
                                   else ()) if isinstance(v, str)])
            for ref in refs:
                if not isinstance(ref, str) or _PLUGIN_ROOT_VAR not in ref:
                    continue
                cands = _candidates(ref)
                if not cands:
                    findings.append(
                        f"{rel_mcp} [{server}]: non-prefix "
                        f"${{CLAUDE_PLUGIN_ROOT}} use in {ref!r}")
                    continue
                for cand in cands:
                    remainder = cand[len(_PLUGIN_ROOT_VAR) + 1:]
                    norm = os.path.normpath(remainder)
                    if os.path.isabs(norm) or norm == ".." or \
                            norm.startswith(".." + os.sep):
                        findings.append(
                            f"{rel_mcp} [{server}]: {ref!r} escapes the "
                            "plugin root (absolute or ..-traversal)")
                        continue
                    head_path = (norm if mcp_dir == "."
                                 else f"{mcp_dir}/{norm}")
                    in_head = _git_lines(root, "ls-tree",
                                         "--name-only", "HEAD", "--",
                                         head_path)
                    if in_head is None:
                        findings.append(
                            f"{rel_mcp} [{server}]: cannot establish that "
                            f"{ref!r} is in the pushed commit (git error) — "
                            "failing closed")
                    elif not in_head:
                        findings.append(
                            f"{rel_mcp} [{server}]: {ref!r} is not in the "
                            "pushed commit (untracked, .gitignored — e.g. a "
                            "dev-only venv — or staged but not committed); "
                            "the installed artifact will not contain it")
    return findings


def make_self_containment_guard() -> HookCallback:
    """Pre-push grep for §2.0 self-containment anti-patterns."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if input_data.get("tool_name") != "Bash":
            return {}
        cmd = input_data.get("tool_input", {}).get("command", "")
        # Sol r4-3: arm on a `git push` ANYWHERE in the command — env-var
        # prefixes (`FOO=1 git push`), `env`/`command` wrappers, global git
        # options (`git -C . push`), and compound commands (`cd x && git
        # push`) must all scan; only options may sit between `git` and
        # `push` so `git stash push` does not arm. The override is an
        # EXPLICIT `CASA_ALLOW_ANTI_PATTERN=1` assignment (quotes allowed)
        # — auditable: the guard still scans and logs what it waved
        # through. (The previously-advertised `--allow-anti-pattern` git
        # flag was never implemented and would make git itself error.)
        # Option arguments may be quoted and contain spaces (Sol r6-1:
        # `git -C "path with spaces" push`).
        m_push = re.search(
            r"\bgit((\s+-\S+(\s+(\"[^\"]*\"|'[^']*'|[^-\s]\S*))?)*)\s+push\b",
            cmd)
        if not m_push:
            return {}
        override = bool(
            re.search(r"\bCASA_ALLOW_ANTI_PATTERN=([\"']?)1\1(\s|$)", cmd))

        cwd = Path(input_data.get("cwd") or os.getcwd())
        # Sol r5-3: scan the repo the COMMAND targets, not the hook cwd —
        # `cd <path> && git push` re-bases, `git -C <path> push` retargets.
        for m_cd in re.finditer(
                r"(?:^|&&|;)\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)",
                cmd[: m_push.start()]):
            t = m_cd.group(1).strip("'\"")
            cwd = Path(t) if os.path.isabs(t) else cwd / t
        m_c = re.search(r"-C\s+(\"[^\"]+\"|'[^']+'|\S+)", m_push.group(1))
        if m_c:
            t = m_c.group(1).strip("'\"")
            cwd = Path(t) if os.path.isabs(t) else cwd / t
        if not cwd.is_dir():
            return {}

        # Sol r4-7: anchor the tree scan at the REPO ROOT when resolvable —
        # a push from a subdirectory must still see a root README
        # anti-pattern. Fall back to cwd outside a worktree.
        top = await asyncio.to_thread(
            _git_lines, cwd, "rev-parse", "--show-toplevel")
        scan_root = Path(top[0]) if top else cwd

        # M28: the walk+read blocks the shared event loop — run it off-loop.
        findings = await asyncio.to_thread(
            _scan_tree_for_anti_patterns, scan_root)
        findings += await asyncio.to_thread(_scan_mcp_launch_refs, cwd)

        if findings:
            if override:
                _logger.warning(
                    "self_containment_guard override "
                    "(CASA_ALLOW_ANTI_PATTERN=1): allowing push despite: %s",
                    "; ".join(findings))
                return {}
            return _deny(
                "Blocked by self_containment_guard (§2.0 axiom):\n"
                + "\n".join(f"- {fi}" for fi in findings)
                + "\nDeclare via casa.systemRequirements or use ${CLAUDE_PLUGIN_ROOT}. "
                "If (and only if) this is a false positive, re-run as "
                "`CASA_ALLOW_ANTI_PATTERN=1 git push ...` — the override is "
                "logged."
            )
        return {}

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


def build_policy_callbacks_from_hooks_yaml(
    hooks_yaml_data: dict,
) -> dict[str, tuple[str, "HookCallback"]]:
    """Build ``{policy_name: (matcher, callback)}`` from an executor hooks.yaml.

    H3 (v0.53.0): the claude_code HTTP hook path (hook_proxy.sh -> the
    /hooks/resolve endpoint) previously ran only the default-configured
    factories, dropping every per-executor ``hooks.yaml`` parameter (e.g.
    plugin-developer's ``path_scope`` writable/readable prefixes), so the
    default empty-prefix ``path_scope`` denied ALL Read/Write/Edit. This turns
    an executor's parsed ``hooks.yaml`` into the same ``(matcher, callback)``
    shape ``_build_cc_hook_policies`` produces, but with the declared params
    applied.

    Policies with no HOOK_POLICIES factory (e.g. ``engagement_permission_relay``
    — injected separately in casa_core with live deps) are skipped here. The
    per-entry ``matcher``/``timeout`` keys are consumed by other paths and are
    not factory parameters, so they are stripped before the factory call.
    """
    out: dict[str, tuple[str, "HookCallback"]] = {}
    for entry in (hooks_yaml_data.get("pre_tool_use") or []):
        name = entry.get("policy")
        policy = HOOK_POLICIES.get(name)
        if policy is None:
            continue  # e.g. engagement_permission_relay — wired separately
        params = {
            k: v for k, v in entry.items()
            if k not in ("policy", "matcher", "timeout")
        }
        out[name] = (policy["matcher"], policy["factory"](**params))
    return out


# ---------------------------------------------------------------------------
# v0.37.2 (C-1): engagement_permission_relay
#
# Spec: docs/superpowers/specs/2026-05-13-c1-permission-relay-fix.md §4.2, §4.3
#
# A PreToolUse hook that resolves the engagement from cwd, checks the
# engagement's frozen ``tools_allowed`` snapshot, and either passes through
# (return ``{}``) or relays the request to the operator via a Telegram
# inline keyboard, awaiting their verdict on a per-engagement asyncio.Queue.
# ---------------------------------------------------------------------------


# Telegram callback_data is capped at 64 bytes; the keyboard prefix is
# "perm:allow:" or "perm:deny:" (11 bytes), so the request_id must be
# <= 53 bytes. We cap at 32 to leave headroom and align with hex UUIDs.
_RID_MAX_LEN = 32


_ENG_CWD_RE = re.compile(
    r"^/data/engagements/([0-9a-f]{32})(?:/.*)?$"
)


def _engagement_id_from_cwd(cwd: str) -> str | None:
    """Extract the 32-hex engagement id from a cwd path.

    Returns None when cwd is not under ``/data/engagements/<id>/...``.
    """
    m = _ENG_CWD_RE.match(cwd or "")
    return m.group(1) if m else None


def _perm_keyboard_finish(
    telegram_channel: Any, topic_id: int | None, message_id: int,
) -> Callable[[dict], "Awaitable[None]"]:
    """Broker finish-hook (r3-B3): keyboard-edit owner for the permission
    namespace. Fires exactly once on outcome (delivered by the broker even if
    the creating hook task was cancelled) and edits the posted keyboard
    message to reflect the outcome. The callback path NEVER edits the
    keyboard — this is the only writer.
    """

    async def _finish(outcome: dict) -> None:
        try:
            await telegram_channel.edit_perm_keyboard_outcome(
                topic_id=topic_id, message_id=message_id, outcome=outcome,
            )
        except Exception:  # noqa: BLE001 — finish hooks must never raise
            _logger.warning(
                "permission keyboard finish-hook edit failed "
                "(topic=%s message_id=%s)", topic_id, message_id, exc_info=True,
            )

    return _finish


# ---------------------------------------------------------------------------
# R4 (v0.89.0, buttons-always): engagement_buttons_reminder
#
# A PreToolUse(Skill) salience backstop on the WORKSPACE hook path
# (hook_proxy.sh -> /internal/hooks/resolve). The plugin-developer engaged
# executor runs the STANDALONE Claude CLI, so an in-casa SDK ``can_use_tool``
# PreToolUse hook never fires for it — this HTTP-path policy is the only seam.
#
# When a ``Skill`` is about to load (e.g. ``superpowers:brainstorming``, whose
# own "present options conversationally / one question per message" HARD-GATE
# out-competes doctrine read earlier in the turn) AND the cwd resolves to an
# ACTIVE engagement, inject a PreToolUse ``additionalContext`` reminder that the
# engagement channel's choice questions ALWAYS use ``ask``/``options``. This is
# context injection, not a block/ask decision, and NOT a user-facing
# ``systemMessage``. Trigger is tool IDENTITY (Skill) + engagement-from-cwd —
# NEVER message content.
# ---------------------------------------------------------------------------

_BUTTONS_REMINDER_TEXT = (
    "You are in an engagement channel — any question offering choices MUST "
    "use the `ask` tool with `options` (tappable buttons), never prose, even "
    "if this skill tells you to ask conversationally."
)


def make_engagement_buttons_reminder(
    *,
    engagement_registry: Any,
) -> HookCallback:
    """Build the PreToolUse(Skill) hook that injects the buttons-always
    reminder when a Skill loads inside an ACTIVE engagement.

    Args:
        engagement_registry: registry exposing ``.get(engagement_id) -> record | None``.

    Returns ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "additionalContext": <reminder>}}`` (SDK-declared
    ``PreToolUseHookSpecificOutput.additionalContext``) for a Skill call under
    an active engagement; ``{}`` (allow, no context) otherwise. Never blocks.
    """

    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        # Tool-identity gate (defense-in-depth; the wired matcher is "Skill"
        # too). NEVER inspect message/tool content.
        if input_data.get("tool_name") != "Skill":
            return {}
        eng_id = _engagement_id_from_cwd(input_data.get("cwd") or "")
        if eng_id is None:
            return {}
        rec = engagement_registry.get(eng_id)
        if rec is None or getattr(rec, "status", None) != "active":
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _BUTTONS_REMINDER_TEXT,
            }
        }

    return _hook


def make_engagement_permission_relay(
    *,
    engagement_registry: Any,
    telegram_channel: Any,
    queues: dict | None = None,
    timeout_s: float = 600.0,
) -> HookCallback:
    """Build the PreToolUse hook that relays non-allow-listed tool calls
    through the engagement's Telegram inline-keyboard, via the
    Casa-owned ``verdict_broker`` (W5/Sol B3,B4).

    Args:
        engagement_registry: registry exposing ``.get(engagement_id) -> record | None``.
        telegram_channel: object with async ``update_topic_state(*, engagement_id, new_state)``,
            ``post_perm_keyboard(*, engagement_id, request_id, tool_name, tool_input) -> int | None``,
            and ``edit_perm_keyboard_outcome(*, topic_id, message_id, outcome)``.
        queues: DEPRECATED — accepted-and-ignored (one release, v0.75.0). The
            operator verdict now flows through ``verdict_broker.BROKER``
            (namespace ``"permission"``), delivered by
            ``channel_handlers._make_permission_verdict``. Kept as a no-op
            kwarg so callers mid-migration don't crash on an unexpected
            keyword; remove once every wiring site has dropped it.
        timeout_s: how long to wait for the operator before treating as deny.
    """
    del queues  # deprecated, accepted-and-ignored (see docstring)

    async def _hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        cwd = input_data.get("cwd") or ""
        eng_id = _engagement_id_from_cwd(cwd)
        if eng_id is None:
            return _deny("engagement context not found")
        rec = engagement_registry.get(eng_id)
        if rec is None or getattr(rec, "status", None) != "active":
            return _deny(
                f"unknown or inactive engagement: {eng_id[:8]}"
            )
        # G-1 v0.37.7: short-circuit on autonomous permission modes. The
        # executor's ``permission_mode`` in definition.yaml encodes operator
        # intent; when ``auto`` or ``bypassPermissions`` the CC CLI is meant
        # to proceed without operator approval — surfacing a Telegram
        # keyboard would defeat the purpose (and hang the engagement when no
        # operator is at the keyboard). ``acceptEdits`` and ``default`` still
        # fall through to the allow-list + relay path.
        mode = getattr(rec, "permission_mode", "acceptEdits") or "acceptEdits"
        if mode in ("auto", "bypassPermissions"):
            return {}
        # Allow-list snapshot from engagement creation (spec §3.5).
        allowed = tuple(getattr(rec, "tools_allowed", ()) or ())
        if matches_any(
            allowed,
            input_data.get("tool_name", ""),
            input_data.get("tool_input") or {},
        ):
            return {}  # pass-through: CC's allow-rule approves

        # Not allow-listed — post inline keyboard and await operator verdict
        # via the broker.
        cc_tool_use_id = input_data.get("tool_use_id") or ""
        rid = cc_tool_use_id[:_RID_MAX_LEN] or uuid.uuid4().hex
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}

        await telegram_channel.update_topic_state(
            engagement_id=eng_id, new_state="awaiting",
        )
        from verdict_broker import BROKER

        req, created = BROKER.register(
            namespace="permission", scope=eng_id, request_id=rid,
            timeout_s=timeout_s,
        )
        outcome: dict[str, Any] = {}
        try:  # r7-B3: whole lifecycle guarded — restore 'active' on any exit
            if created:
                # STATIC meta BEFORE posting so a fast tap never sees
                # incomplete metadata (r3-B3). message_id + finish_hook are
                # set by the broker-owned setup task (r8-B3).
                req.meta.update({
                    "options": ["allow", "deny"],
                    "topic_id": rec.topic_id,
                    "operator_id": rec.origin.get("user_id"),
                })

            # The keyboard post — a broker-owned SHIELDED setup task (r8-B3):
            # cancelling THIS hook never interrupts an in-flight Telegram post
            # (which may already be accepted server-side), so a same-id retry
            # never produces a second keyboard. Post FAILURE inside the task
            # unregisters (waiters get delivery_failed).
            async def _post_keyboard() -> int | None:
                await BROKER.ensure_posted(
                    req,
                    lambda: telegram_channel.post_perm_keyboard(
                        engagement_id=eng_id, request_id=rid,
                        tool_name=tool_name, tool_input=tool_input),
                    lambda mid: _perm_keyboard_finish(
                        telegram_channel, rec.topic_id, mid),
                )
                return req.meta.get("message_id")

            try:
                # v0.79.0 (§2 F1(a)): the permission keyboard is a DISCRETE send
                # and MUST go through the single writer — never eager-post around
                # the sequencer. Register+arm a send INTENT fenced on the GATED
                # tool's own frame (hash = identity over the raw tool_input), and
                # the relay posts the keyboard at that block (sealing preceding
                # narration first); a late intent posts out-of-band through the
                # sequencer's watcher. Only the CREATED (first-attempt) intent
                # installs the poster + arms; a retry rides the first attempt's
                # post and just awaits the same broker verdict below. No live
                # sequencer / degraded boot ⇒ eager fallback (pre-v0.79 post).
                _relay_posted = False
                _drv = _active_claude_code_driver()
                if _drv is not None:
                    from channels.output_sequencer import (
                        projection_hash as _perm_projection_hash,
                    )
                    _phash = _perm_projection_hash(tool_name, tool_input)
                    _res = _drv.register_send_intent(
                        engagement_id=eng_id, request_id=rid,
                        tool_name=tool_name, projection_hash=_phash,
                        poster=_post_keyboard,
                    )
                    if _res is not None:
                        _intent, _created_intent = _res
                        if _created_intent:
                            # First attempt: install the real poster + ARM — the
                            # relay posts the keyboard at the gated tool's frame.
                            _drv.set_send_intent_poster(eng_id, rid, _post_keyboard)
                            _drv.arm_send_intent(eng_id, rid)
                        # F1 (Sol r2): whether we just CREATED the intent or
                        # REATTACHED to an existing one (a permission/transport
                        # RETRY, created=False), the relay owns the post — NEVER
                        # eager-post around the sequencer. A retry rides the first
                        # attempt's keyboard and awaits the same broker verdict
                        # below. Eager fallback ONLY when there is no live
                        # sequencer (register returned None).
                        _relay_posted = True
                if not _relay_posted:
                    await _post_keyboard()
                outcome = await BROKER.await_result(req)
                if outcome.get("outcome") == "delivery_failed":
                    return _deny("keyboard post failed")
            except asyncio.CancelledError:
                # r4-B3: single in-process awaiter, no reattach —
                # cancellation IS logical cancel (during post OR await). The
                # setup task completes in the background; BROKER.cancel
                # resolves the request, and the finish-hook (installed by
                # setup even after completion — r4-B1) edits the keyboard to
                # "expired". NOT engagement-terminal.
                BROKER.cancel(
                    namespace="permission", scope=eng_id, request_id=rid,
                    reason="tool_invocation_cancelled",
                )
                raise
        finally:
            # r7-B3: restore topic state on EVERY exit — post failure,
            # cancellation during post or await, or normal completion.
            await telegram_channel.update_topic_state(
                engagement_id=eng_id, new_state="active",
            )
        o = outcome.get("outcome")
        if o == "answered" and outcome.get("option_index") == 0:
            return {}
        if o == "no_answer":
            return _deny("operator did not respond within the window")
        return _deny("Operator denied via Telegram")

    return _hook
