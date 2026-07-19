"""Immutable content-addressed plugin artifact store (spec §3.2).

Publish pipeline: resolve ref -> fetch git archive of the exact commit
(bare fetch, never a mutable working clone) -> validate in staging ->
checksum -> metadata INSIDE staging -> atomic rename into the store.
STDLIB-ONLY: imported by the Dockerfile build helper before any venv.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from plugin_registry import STORE_ROOT, compute_artifact_id, normalize_subdir
from text_util import is_unsafe_text, sanitize_segment

logger = logging.getLogger(__name__)

_PROTECTED_TOOL_SUMMARY_MAX_CHARS = 200

STAGING_ROOT = Path("/config/plugins/.staging")
METADATA_FILENAME = ".casa-artifact.json"


class StoreError(Exception):
    reason_code = "store_error"

    def __init__(self, message: str, *, reason_code: str | None = None,
                 detail: dict | None = None):
        super().__init__(message)
        if reason_code is not None:
            self.reason_code = reason_code
        # Machine-readable payload extras the tool layer surfaces verbatim —
        # e.g. name_mismatch carries the canonical manifest_name so the
        # configurator self-corrects in one retry (Sol v093-1).
        self.detail: dict = dict(detail or {})


class RefNotFound(StoreError):
    reason_code = "ref_not_found"


class ResolveUnavailable(StoreError):
    """Transient/retryable resolve verdict. C.3 (v0.74.0): carries structured
    retry metadata — ``retry_after_s`` is the server's latest Retry-After (or
    the wait the resolver refused to truncate) so callers can surface it."""
    reason_code = "resolve_unavailable"

    def __init__(self, message: str, *, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ResolveAuthFailed(StoreError):
    """C.1 (v0.74.0): 401 / non-rate-limited 403 — hard, non-retryable."""
    reason_code = "resolve_auth_failed"


class SourceEmpty(StoreError):
    """C.1 (v0.74.0): 409 'repository is empty' — hard, non-retryable."""
    reason_code = "source_empty"


# C.2/A.2 (v0.74.0): a release ref is exactly "v" + semver.
RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def normalize_revision(rev) -> str | None:
    """Canonical revision form (spec §E): lowercase 40-hex sha. Accepts the
    registry's ``git:<sha>`` form and a bare sha; anything else -> None."""
    if not isinstance(rev, str):
        return None
    r = rev.strip().lower()
    if r.startswith("git:"):
        r = r[4:]
    return r if _HEX40_RE.match(r) else None


def _entry_line(rel: str, etype: str, exec_bit: int, payload: str) -> bytes:
    # Length-framed over UTF-8 BYTES (not str chars — multibyte paths would
    # otherwise produce ambiguous frames).
    body = f"{rel}\x00{etype}\x00{exec_bit}\x00{payload}".encode("utf-8")
    return str(len(body)).encode("ascii") + b":" + body


def content_checksum(root: Path) -> str:
    root = Path(root)
    lines: list[bytes] = []
    entries = sorted(
        p for p in root.rglob("*")
        if p.relative_to(root).as_posix() != METADATA_FILENAME
    )
    for p in entries:
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        exec_bit = 1 if (st.st_mode & stat.S_IXUSR) else 0
        if stat.S_ISLNK(st.st_mode):
            lines.append(_entry_line(rel, "l", 0, os.readlink(p)))
        elif stat.S_ISDIR(st.st_mode):
            lines.append(_entry_line(rel, "d", 0, ""))
        elif stat.S_ISREG(st.st_mode):
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            lines.append(_entry_line(rel, "f", exec_bit, h))
        else:
            raise StoreError(f"special file in artifact: {rel}",
                             reason_code="unsafe_archive")
    return hashlib.sha256(b"".join(lines)).hexdigest()


def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            name = PurePosixPath(m.name)
            if name.is_absolute() or ".." in name.parts:
                raise StoreError(f"unsafe path: {m.name}",
                                 reason_code="unsafe_archive")
            if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise StoreError(f"special file: {m.name}",
                                 reason_code="unsafe_archive")
            if m.issym() or m.islnk():
                target = PurePosixPath(m.linkname)
                if target.is_absolute():
                    raise StoreError(f"absolute link: {m.name}",
                                     reason_code="unsafe_archive")
                joined = (name.parent / target)
                # Normalize and require it stays inside the artifact root.
                parts: list[str] = []
                for part in joined.parts:
                    if part == "..":
                        if not parts:
                            raise StoreError(f"escaping link: {m.name}",
                                             reason_code="unsafe_archive")
                        parts.pop()
                    elif part != ".":
                        parts.append(part)
        # `filter="data"` (PEP 706) is defense-in-depth ON TOP of the per-member
        # validation above. It exists on Python 3.12+ and the 3.9-3.11 security
        # backports (3.11.4+), but NOT on older 3.11 — which is what the add-on's
        # base image ships (the unit gate runs a 3.12 venv, so only the image
        # build catches this). Fall back to a plain extract where the kwarg is
        # unavailable; the validation loop is the actual safety net.
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            tf.extractall(dest)


def write_metadata(root: Path, *, name: str, repo: str, ref: str,
                   revision: str, subdir: str, artifact_id: str,
                   version: str, checksum: str) -> None:
    meta = {
        "schema_version": 1,
        "name": name, "repo": repo, "ref": ref, "revision": revision,
        "subdir": normalize_subdir(subdir), "artifact_id": artifact_id,
        "version": version, "content_checksum": checksum,
    }
    p = Path(root) / METADATA_FILENAME
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def parse_mcp_servers(mcp_json_path: Path) -> tuple[dict, bool]:
    """THE single shared ``.mcp.json`` parser (Sol) — used by grant derivation,
    secret extraction, verification's malformed check, AND the build-time
    verifier, so all four agree on both shapes.

    Returns ``(valid_servers, malformed)``. Handles BOTH the project-style
    ``{"mcpServers": {...}}`` wrapper AND the top-level form real plugins use
    (e.g. context7's ``{"context7": {"command": "npx", ...}}`` — no wrapper). A
    server is VALID only if its config declares a non-empty-string ``command``
    OR ``url`` (the load-bearing launch fields); ``args``/``env``/``type`` alone
    do NOT make a runnable server. ``malformed`` is True when the file is PRESENT
    but unparseable / not an object / declares a non-dict ``mcpServers`` / OR
    declares ANY server-like object that is not valid (a broken server blocks
    readiness even beside a valid sibling — Sol). An ABSENT file (skill-only
    plugin) and a config declaring ZERO servers (``{"mcpServers": {}}``) are NOT
    malformed; a DECLARED server that is non-dict or lacks a runnable
    command/url IS. Wrapper keys still derive grants while malformed gates
    readiness."""
    path = Path(mcp_json_path)
    if not path.is_file():
        return {}, False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("plugin .mcp.json unreadable (%s): %s", path, exc)
        return {}, True
    return parse_mcp_servers_text(text, source=str(path))


def parse_mcp_servers_text(text: str, *, source: str = "<text>",
                           ) -> tuple[dict, bool]:
    """:func:`parse_mcp_servers` over already-loaded content — for callers
    whose ``.mcp.json`` does not live on disk (Sol r5-2: the pre-push guard
    reads the PUSHED commit's file via ``git show``, never the worktree)."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        logger.debug("plugin .mcp.json unparseable (%s): %s", source, exc)
        return {}, True
    if not isinstance(data, dict):
        logger.debug("plugin .mcp.json not an object (%s)", source)
        return {}, True
    def _runnable(cfg) -> bool:
        # A runnable server declares a non-empty command (stdio) OR url (http/sse).
        if not isinstance(cfg, dict):
            return False
        # Sol r4-1: a declared `args` must be list[str] — any other shape made
        # mcp_command_verdicts raise mid-§3.9 (after activation committed,
        # before health regen). Malformed args ⇒ not runnable ⇒ mcp_invalid.
        args = cfg.get("args")
        if args is not None and (not isinstance(args, list)
                                 or any(not isinstance(a, str) for a in args)):
            return False
        return (
            bool(cfg.get("command")) and isinstance(cfg.get("command"), str)
            or bool(cfg.get("url")) and isinstance(cfg.get("url"), str))

    if "mcpServers" in data:
        # WRAPPER form: `mcpServers` is the explicit intent signal (KEY presence
        # — so `{"mcpServers": null}` is caught here, not mistaken for top-level).
        # Grants derive from every dict entry's KEY; but malformed is set when ANY
        # declared entry is non-dict or not runnable, so a broken server still
        # blocks readiness (Sol).
        servers = data["mcpServers"]
        if not isinstance(servers, dict):
            logger.debug("plugin .mcp.json mcpServers not a mapping (%s)",
                         source)
            return {}, True
        grant_servers = {k: v for k, v in servers.items() if isinstance(v, dict)}
        malformed = any(not _runnable(v) for v in servers.values())
        return grant_servers, malformed
    # TOP-LEVEL form (no wrapper): with no intent signal, only entries declaring
    # command|url are servers; a server-like object (any dict candidate) lacking
    # BOTH means the file is malformed — even if a sibling entry is valid (Sol).
    candidates = {k: v for k, v in data.items() if isinstance(v, dict)}
    valid = {k: v for k, v in candidates.items() if _runnable(v)}
    malformed = any(not _runnable(v) for v in candidates.values())
    return valid, malformed


def mcp_servers_map(mcp_json_path: Path) -> dict:
    """The valid {server-name: config} map — see :func:`parse_mcp_servers`."""
    return parse_mcp_servers(mcp_json_path)[0]


_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
_PLUGIN_DATA_VAR = "${CLAUDE_PLUGIN_DATA}"

# G6 corrected (2026-07-19, Sol v095 + CLI probe): these are CLI-RESERVED —
# the Claude CLI natively injects a per-plugin value into every plugin MCP
# server's environment. A plugin that SELF-DECLARES one of them in
# ``.mcp.json::env`` shadows the native value; the self-referential form
# (``"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"``) delivers the LITERAL
# placeholder string (the gmail-v0.4.0 token-in-a-literal-directory bug).
RESERVED_PLUGIN_ENV_KEYS: frozenset[str] = frozenset({
    "CLAUDE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT",
})


def reserved_env_violations(mcp_json_path: Path) -> list[str]:
    """Servers whose ``env`` self-declares a CLI-reserved variable (any
    value — the KEY alone shadows the CLI's native provision). One
    human-readable string per violation; empty when clean or file absent."""
    violations: list[str] = []
    for name, cfg in mcp_servers_map(Path(mcp_json_path)).items():
        env = cfg.get("env")
        for key in (env.keys() if isinstance(env, dict) else ()):
            if key in RESERVED_PLUGIN_ENV_KEYS:
                violations.append(
                    f"[{name}]: env self-declares CLI-reserved {key} — the "
                    "CLI provides it natively; declaring it shadows the "
                    "native value (self-reference yields the literal "
                    "placeholder)")
    return violations


def mcp_command_verdicts(mcp_json_path: Path, plugin_root: Path | str,
                         *, _which=None) -> list[dict]:
    """Static resolvability check of a plugin's ``.mcp.json`` launch
    references (2026-07-18 plan P1).

    Detects *resolvable command and artifact-file references* ONLY — NOT
    "the server can spawn": missing imports, a bad shebang, a wrong working
    directory, or a dead external service all still pass this check (a live
    handshake probe is an explicit non-goal). ``url`` servers are skipped.

    Shapes the check cannot judge report ``status: "unchecked"`` and never
    block: shell-form commands (whitespace — single-executable-token support
    only), relative paths (resolve against the CLI's spawn cwd, unknowable
    here), ``${CLAUDE_PLUGIN_DATA}`` (runtime-populated), and any other
    ``${VAR}``-dependent reference.

    Rows: ``{"server", "ref", "status": "ok"|"missing"|"unchecked",
    "reason"?}``. ``_which`` is a test seam for PATH resolution (mirrors
    verify's ``_tools_bin``).
    """
    which = _which if _which is not None else shutil.which
    root_path = Path(plugin_root)
    root = str(root_path)
    rows: list[dict] = []

    def _row(server: str, ref: str, status: str, reason: str | None = None):
        row = {"server": server, "ref": ref, "status": status}
        if reason:
            row["reason"] = reason
        rows.append(row)

    def _escapes_root(resolved: str) -> bool:
        # Sol r4-9: containment — a root-anchored reference must stay inside
        # the checksummed artifact AFTER symlink resolution; verify must not
        # bless mutable sibling/system content reachable via `..` or an
        # escaping symlink. resolve() is non-strict: a merely-missing path
        # under the root resolves under the root and falls through to the
        # existence check.
        try:
            rp = Path(resolved).resolve()
            rootp = root_path.resolve()
        except OSError:
            return True
        return not (rp == rootp or rootp in rp.parents)

    def _check_root_path(server: str, ref: str, candidate: str,
                         *, require_exec: bool) -> None:
        resolved = candidate.replace(_PLUGIN_ROOT_VAR, root)
        if _ENV_VAR_RE.search(resolved):
            return  # env-dependent — cannot judge, never block
        if _escapes_root(resolved):
            _row(server, ref, "missing",
                 f"{candidate!r} escapes the plugin root "
                 "(traversal or symlink)")
            return
        p = Path(resolved)
        if require_exec:
            if not p.is_file():
                _row(server, ref, "missing", f"{resolved} does not exist")
            elif not os.access(p, os.X_OK):
                _row(server, ref, "missing", f"{resolved} not executable")
            else:
                _row(server, ref, "ok")
        # Sol r4-8: non-exec references may be files OR directories
        # (`--directory ${ROOT}/server`, PYTHONPATH vendor dir).
        elif p.is_file() or p.is_dir():
            _row(server, ref, "ok")
        else:
            _row(server, ref, "missing", f"{resolved} does not exist")

    def _check_command(server: str, command: str) -> None:
        if command.strip() != command or " " in command or "\t" in command:
            _row(server, command, "unchecked",
                 "shell-form command (single executable token only)")
            return
        if _PLUGIN_DATA_VAR in command:
            _row(server, command, "unchecked",
                 "references runtime-populated ${CLAUDE_PLUGIN_DATA}")
            return
        if command.startswith(_PLUGIN_ROOT_VAR + "/"):
            _check_root_path(server, command, command, require_exec=True)
            return
        if _PLUGIN_ROOT_VAR in command:
            _row(server, command, "unchecked",
                 "non-prefix ${CLAUDE_PLUGIN_ROOT} use")
            return
        leftover = _ENV_VAR_RE.search(command)
        if leftover:
            _row(server, command, "unchecked",
                 f"env-dependent command (${{{leftover.group(1)}}})")
            return
        if os.path.isabs(command):
            p = Path(command)
            if not p.is_file():
                _row(server, command, "missing", f"{command} does not exist")
            elif not os.access(p, os.X_OK):
                _row(server, command, "missing", f"{command} not executable")
            else:
                _row(server, command, "ok")
            return
        if os.sep in command:
            _row(server, command, "unchecked",
                 "relative path resolves against the spawn cwd")
            return
        if which(command) is None:
            _row(server, command, "missing", f"{command!r} not found in PATH")
        else:
            _row(server, command, "ok")

    def _path_candidates(ref: str) -> list[str]:
        """Root-anchored path substrings of a reference: the whole ref, the
        value of an embedded `--opt=` (Sol r4-8), or `:`-joined segments
        (PYTHONPATH-style env values, Sol r4-6)."""
        return [chunk for chunk in re.split(r"[=:]", ref)
                if chunk.startswith(_PLUGIN_ROOT_VAR + "/")]

    for name, cfg in mcp_servers_map(Path(mcp_json_path)).items():
        command = cfg.get("command")
        if not isinstance(command, str) or not command:
            continue  # url server (or non-stdio) — skipped
        _check_command(name, command)
        args = cfg.get("args")
        for arg in (args if isinstance(args, list) else []):
            if not isinstance(arg, str) or _PLUGIN_ROOT_VAR not in arg:
                continue
            for cand in _path_candidates(arg):
                _check_root_path(name, arg, cand, require_exec=False)
        env = cfg.get("env")
        for key, val in (env.items() if isinstance(env, dict) else ()):
            if not isinstance(val, str) or _PLUGIN_ROOT_VAR not in val:
                continue
            for cand in _path_candidates(val):
                _check_root_path(name, f"env[{key}]={cand}", cand,
                                 require_exec=False)
    return rows


def read_metadata(root: Path) -> dict | None:
    try:
        return json.loads((Path(root) / METADATA_FILENAME)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def validate_artifact(path: Path) -> bool:
    meta = read_metadata(path)
    if not isinstance(meta, dict):
        return False
    try:
        return content_checksum(Path(path)) == meta.get("content_checksum")
    except (OSError, StoreError):
        return False


def artifact_verdict(path: Path, *, name: str, repo: str, revision: str,
                     subdir: str, artifact_id: str) -> str | None:
    """Deep validation against the EXPECTED identity (Sol R2-1).
    None = fully valid; 'artifact_invalid' = metadata/manifest/identity
    problem; 'corrupt_artifact' = identity fine but content checksum fails."""
    meta = read_metadata(path)
    if not isinstance(meta, dict):
        return "artifact_invalid"
    from plugin_registry import normalize_repo as _nrepo
    identity_ok = (
        meta.get("artifact_id") == artifact_id
        and meta.get("name") == name
        and _nrepo(str(meta.get("repo", ""))) == _nrepo(repo)
        and meta.get("revision") == revision
        and meta.get("subdir") == normalize_subdir(subdir)
    )
    if not identity_ok:
        return "artifact_invalid"
    try:
        manifest = json.loads((Path(path) / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "artifact_invalid"
    if not isinstance(manifest, dict) or manifest.get("name") != name:
        return "artifact_invalid"
    try:
        if content_checksum(Path(path)) != meta.get("content_checksum"):
            return "corrupt_artifact"
    except (OSError, StoreError):
        return "corrupt_artifact"
    # A:§3.7 (r2-B6/r3-4 upgrade path): re-check pre-v0.76 stored artifacts —
    # a present-but-malformed casa.protectedTools excludes THIS artifact from
    # resolution (per-plugin degradation), never a whole-role failure.
    try:
        manifest_protected_tools(manifest)
    except StoreError:
        return "protected_tools_invalid"
    return None


_REJECTED_REQ_TYPES = {"apt", "dpkg", "yum", "dnf", "pacman"}


@dataclass(frozen=True)
class PublishResult:
    name: str
    artifact_id: str
    revision: str
    version: str
    path: str
    manifest: dict


def _parse_gh_http_response(stdout: str) -> tuple[int | None, dict, str]:
    """Parse `gh api -i` stdout into (status, headers, body). `-i` prepends
    the status line + headers to the body on stdout for BOTH success and
    error responses (live-verified 2026-07-13: 422 → status line + headers +
    JSON body, exit 1). Tolerates stacked header blocks (1xx/redirects): the
    LAST status line wins. Header keys are lowercased. (None, {}, stdout)
    when no HTTP status line is present — a tooling failure, classified
    retryable by the caller."""
    status: int | None = None
    headers: dict[str, str] = {}
    lines = stdout.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith("HTTP/"):
        parts = lines[i].split()
        try:
            status = int(parts[1])
        except (IndexError, ValueError):
            return None, {}, stdout
        headers = {}
        i += 1
        while i < len(lines) and lines[i].strip():
            k, _, v = lines[i].partition(":")
            headers[k.strip().lower()] = v.strip()
            i += 1
        i += 1  # consume the blank separator
    if status is None:
        return None, {}, stdout
    return status, headers, "\n".join(lines[i:])


def gh_api_probe(path: str, *, timeout: float = 20.0, accept: str | None = None,
                 jq: str | None = None) -> tuple[int, dict, str]:
    """One structured GitHub-API round trip via `gh api -i` (C.1: status +
    headers + body — never stderr-grep). ``jq`` applies only to the 2xx body
    (gh leaves error bodies as raw JSON). Raises ResolveUnavailable on
    transport/tooling failure. Shared by resolve_ref and the A.2 completion
    guard so producer-verify and configurator-pin agree by construction."""
    argv = ["gh", "api", "-i", path]
    if accept:
        argv += ["-H", f"Accept: {accept}"]
    if jq:
        argv += ["--jq", jq]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ResolveUnavailable(f"gh api timeout for {path}") from exc
    except (FileNotFoundError, OSError) as exc:
        raise ResolveUnavailable(f"gh unavailable: {exc}") from exc
    status, headers, body = _parse_gh_http_response(proc.stdout or "")
    if status is None:
        err = ((proc.stderr or "") + (proc.stdout or "")).strip()[:200]
        raise ResolveUnavailable(f"gh api failed for {path}: {err}")
    return status, headers, body


_GH_NO_COMMIT_RE = re.compile(r"no commit found for sha", re.IGNORECASE)
_GH_EMPTY_REPO_RE = re.compile(r"repository is empty", re.IGNORECASE)
_GH_RATE_LIMIT_BODY_RE = re.compile(
    r"(secondary rate limit|exceeded a secondary rate limit|"
    r"rate limit exceeded|api rate limit)", re.IGNORECASE)

_RESOLVE_MAX_ATTEMPTS = 3
_RESOLVE_WAIT_BUDGET_S = 60.0
_RESOLVE_DEFAULT_BACKOFF_S = (2.0, 8.0)   # pre-attempt-2, pre-attempt-3


def _rate_limited(status: int, headers: dict, body: str) -> bool:
    """Rate-limit detection needs HEADERS (why C.1 mandates structured
    capture); body text is the fallback for secondary limits (C.3)."""
    if status == 429:
        return True
    if status != 403:
        return False
    if headers.get("x-ratelimit-remaining") == "0":
        return True
    if "retry-after" in headers:
        return True
    return bool(_GH_RATE_LIMIT_BODY_RE.search(body or ""))


def _retry_after_seconds(headers: dict) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _classify_resolve_failure(repo: str, ref: str, status: int,
                              headers: dict, body: str) -> StoreError:
    """C.1 verdict table for repos/<repo>/commits/<ref>. Returns the exception
    INSTANCE (the caller decides raise-vs-retry for ResolveUnavailable)."""
    if status == 422 and _GH_NO_COMMIT_RE.search(body or ""):
        return RefNotFound(f"{repo}@{ref} not found (HTTP 422: no such ref)")
    if status == 404:
        return RefNotFound(
            f"{repo}@{ref}: repository or ref not visible (HTTP 404)")
    if status == 401 or (status == 403
                         and not _rate_limited(status, headers, body)):
        return ResolveAuthFailed(
            f"GitHub auth failed for {repo}@{ref} (HTTP {status})")
    if status == 409 and _GH_EMPTY_REPO_RE.search(body or ""):
        return SourceEmpty(f"{repo} is an empty repository (HTTP 409)")
    return ResolveUnavailable(
        f"gh api failed for {repo}@{ref}: HTTP {status}: "
        f"{(body or '').strip()[:200]}",
        retry_after_s=_retry_after_seconds(headers))


def _sha_from_body(body: str) -> str | None:
    """Commit sha from a 200 body: `--jq .sha` yields a bare sha; a full-JSON
    body (jq unapplied) is tolerated."""
    out = (body or "").strip()
    if out.startswith("{"):
        try:
            out = str(json.loads(out).get("sha", ""))
        except ValueError:
            return None
    sha = out.strip().strip('"').lower()
    return sha if _HEX40_RE.match(sha) else None


def resolve_ref(repo: str, ref: str, *, timeout: float = 20.0,
                _sleep=time.sleep) -> str:
    """Resolve a ref to a 40-hex commit via the GitHub commits API (annotated
    tags auto-peel to the commit). C.1 classification: 422-no-commit / 404 =>
    RefNotFound; 401 / non-rate-limited 403 => ResolveAuthFailed; 409-empty =>
    SourceEmpty (all hard, pre-mutation); rate-limited 403/429 => bounded
    retry (C.3: <=3 attempts, <=60s TOTAL wait, Retry-After honored fully or
    not at all — never truncated); everything else transient =>
    ResolveUnavailable carrying retry_after_s metadata. NEVER conflated."""
    waited = 0.0
    last_retry_after: float | None = None
    for attempt in range(1, _RESOLVE_MAX_ATTEMPTS + 1):
        status, headers, body = gh_api_probe(
            f"repos/{repo}/commits/{ref}", timeout=timeout, jq=".sha")
        if status == 200:
            sha = _sha_from_body(body)
            if sha is None:
                raise ResolveUnavailable(
                    f"unparseable sha for {repo}@{ref}: {body[:80]!r}")
            return sha
        exc = _classify_resolve_failure(repo, ref, status, headers, body)
        if not isinstance(exc, ResolveUnavailable):
            raise exc                       # hard verdicts never retry
        if not _rate_limited(status, headers, body):
            raise exc                       # transient-but-not-rate-limited:
                                            # surface; the CALLER owns retry
        wait = _retry_after_seconds(headers)
        if wait is not None:
            last_retry_after = wait
        if attempt >= _RESOLVE_MAX_ATTEMPTS:
            raise ResolveUnavailable(
                f"rate-limit retries exhausted for {repo}@{ref}",
                retry_after_s=last_retry_after)
        if wait is None:
            wait = _RESOLVE_DEFAULT_BACKOFF_S[attempt - 1]
        if waited + wait > _RESOLVE_WAIT_BUDGET_S:
            # Honor Retry-After FULLY or not at all — never truncate a
            # server-requested delay and retry early (C.3).
            raise ResolveUnavailable(
                f"gh rate-limited for {repo}@{ref}; server asks "
                f"retry-after: {int(wait)}s which exceeds the remaining "
                f"budget — try again later",
                retry_after_s=wait)
        _sleep(wait)
        waited += wait
    raise ResolveUnavailable(f"resolve retries exhausted for {repo}@{ref}",
                             retry_after_s=last_retry_after)


def fetch_commit_tree(repo: str, commit: str, subdir: str, dest: Path,
                      *, timeout: float = 300.0) -> None:
    """Bare authenticated fetch of the exact commit -> `git archive` -> safe
    extraction. Never a mutable .git working clone. Auth flows through
    /etc/gitconfig + git-credential-casa.sh (GITHUB_TOKEN), anonymous for
    public repos."""
    url = f"https://github.com/{repo}.git"
    subdir = normalize_subdir(subdir)
    with tempfile.TemporaryDirectory(dir=str(Path(dest).parent)) as td:
        bare = Path(td) / "bare.git"
        tar_path = Path(td) / "tree.tar"
        try:
            subprocess.run(["git", "init", "--bare", "-q", str(bare)],
                           check=True, capture_output=True, timeout=60)
            subprocess.run(
                ["git", "-C", str(bare), "fetch", "--depth", "1", "-q",
                 url, commit],
                check=True, capture_output=True, timeout=timeout)
            argv = ["git", "-C", str(bare), "archive",
                    "-o", str(tar_path), commit]
            if subdir:
                argv.append(subdir)
            subprocess.run(argv, check=True, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise ResolveUnavailable(f"git fetch/archive timeout: {repo}"
                                     ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace")[:200]
            raise ResolveUnavailable(
                f"git fetch/archive failed for {repo}@{commit}: {detail}",
            ) from exc
        extract_root = Path(td) / "x"
        safe_extract_tar(tar_path, extract_root)
        src = extract_root / subdir if subdir else extract_root
        if not src.is_dir():
            raise StoreError(f"subdir {subdir!r} absent at {repo}@{commit}",
                             reason_code="manifest_invalid")
        shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)


def manifest_sysreqs(manifest: dict) -> list:
    """Guarded casa.systemRequirements extraction (Sol R2-3): tolerates a
    non-object `casa` and non-list requirements. THE shared helper — used by
    validate_manifest, plugin_add, and plugin_update; never re-derive inline."""
    casa = manifest.get("casa") if isinstance(manifest, dict) else None
    reqs = casa.get("systemRequirements") if isinstance(casa, dict) else None
    return [r for r in reqs if isinstance(r, dict)] if isinstance(reqs, list) else []


def manifest_protected_tools(manifest: dict) -> list:
    """Guarded + STRICT casa.protectedTools extraction (A:§3.7, extended
    v0.78.0 W1), beside manifest_sysreqs. An ABSENT ``casa.protectedTools``
    (no ``casa``, a non-object ``casa``, or the key itself missing) means "no
    protected tools" -> ``[]``.

    Each entry is EITHER a non-empty string (legacy form, meaning: no
    summary) OR an object ``{"name": "<tool>", "summary": "<template>"}``
    with ``name`` a non-empty string and ``summary`` OPTIONAL — when present,
    a non-empty string of at most 200 chars that also passes the
    ``text_util.is_unsafe_text`` UNSAFE-TEXT predicate (must NOT be unsafe).
    Any other shape (wrong type, empty name, non-string/empty/oversized/
    unsafe summary, unknown object keys) is a plugin-author error: raises
    ``StoreError(reason_code="protected_tools_invalid")``.

    DUPLICATE names — equal AFTER ``text_util.sanitize_segment`` (the same
    sanitization ``plugin_grants`` applies when deriving the runtime tool
    id, so e.g. ``"do thing"`` and ``"do_thing"`` collide), across string
    AND object forms, in any order — also raise ``protected_tools_invalid``;
    there is no last-wins summary semantics.

    Each of the THREE call sites decides what a raise means —
    ``validate_manifest`` refuses an install/update, ``artifact_verdict``
    excludes an already-stored artifact from resolution (per-plugin
    degradation, never a whole-role failure), and
    ``plugin_grants.protected_map`` excludes just that plugin's tools from
    the map. B7 operator ruling: semantic typos (a declared name that
    doesn't match a real tool) are an accepted plugin-author trust boundary
    — this validates SHAPE only, never runtime MCP enumeration.

    Returns a NORMALIZED list of ``{"name": str, "summary": str | None}``
    (declaration order preserved); existing callers that only need names
    adapt via ``[e["name"] for e in manifest_protected_tools(manifest)]``.
    """
    casa = manifest.get("casa") if isinstance(manifest, dict) else None
    if not isinstance(casa, dict) or "protectedTools" not in casa:
        return []
    value = casa.get("protectedTools")
    if not isinstance(value, list):
        raise StoreError(
            "casa.protectedTools must be a list",
            reason_code="protected_tools_invalid")

    out: list[dict] = []
    seen_segments: set[str] = set()
    for entry in value:
        if isinstance(entry, str):
            name, summary = entry, None
            if not name:
                raise StoreError(
                    "casa.protectedTools string entry must be non-empty",
                    reason_code="protected_tools_invalid")
        elif isinstance(entry, dict):
            unknown = set(entry) - {"name", "summary"}
            if unknown:
                raise StoreError(
                    f"casa.protectedTools object entry has unknown "
                    f"key(s): {sorted(unknown)!r}",
                    reason_code="protected_tools_invalid")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise StoreError(
                    "casa.protectedTools object entry needs a non-empty "
                    "string 'name'",
                    reason_code="protected_tools_invalid")
            if "summary" in entry:
                summary = entry["summary"]
                if (not isinstance(summary, str) or not summary
                        or len(summary) > _PROTECTED_TOOL_SUMMARY_MAX_CHARS):
                    raise StoreError(
                        "casa.protectedTools object entry 'summary' must "
                        "be a non-empty string of at most "
                        f"{_PROTECTED_TOOL_SUMMARY_MAX_CHARS} chars",
                        reason_code="protected_tools_invalid")
                if is_unsafe_text(summary):
                    raise StoreError(
                        "casa.protectedTools object entry 'summary' "
                        "contains an unsafe control/bidi/line-separator "
                        "codepoint",
                        reason_code="protected_tools_invalid")
            else:
                summary = None
        else:
            raise StoreError(
                "casa.protectedTools entries must be a non-empty string "
                "or an object with 'name'",
                reason_code="protected_tools_invalid")

        segment = sanitize_segment(name)
        if segment in seen_segments:
            raise StoreError(
                f"casa.protectedTools has a duplicate tool after "
                f"sanitization: {name!r}",
                reason_code="protected_tools_invalid")
        seen_segments.add(segment)
        out.append({"name": name, "summary": summary})
    return out


def validate_manifest(root: Path, expected_name: str) -> dict:
    mf = Path(root) / ".claude-plugin" / "plugin.json"
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StoreError(f"plugin.json missing/unparseable: {exc}",
                         reason_code="manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise StoreError("plugin.json not an object",
                         reason_code="manifest_invalid")
    if manifest.get("name") != expected_name:
        raise StoreError(
            f"manifest name {manifest.get('name')!r} != {expected_name!r}",
            reason_code="name_mismatch",
            detail={"manifest_name": manifest.get("name")})
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        # Real-world plugins (e.g. every anthropics/claude-plugins-official
        # plugin) ship NO top-level version. Version is no longer identity-load-
        # bearing — artifact identity is content-addressed and the version-keyed
        # cache that caused the incident is gone — so DEFAULT a missing version
        # rather than reject (the authoring doctrine still recommends one). The
        # unit gate uses versioned fixtures, so only the image build surfaced this.
        manifest["version"] = "0.0.0"
    for req in manifest_sysreqs(manifest):
        if req.get("type") in _REJECTED_REQ_TYPES:
            raise StoreError(
                f"package-manager requirement rejected: {req.get('type')}",
                reason_code="apt_requirements_rejected")
    # A:§3.7 (B7): a PRESENT-but-malformed casa.protectedTools refuses the
    # install/update outright (strict; raises protected_tools_invalid).
    manifest_protected_tools(manifest)
    return manifest


def _stage_and_swap(*, name, repo, ref, revision, subdir, staged: Path,
                    store_root: Path) -> PublishResult:
    """Shared tail: validate -> checksum -> metadata-in-staging -> rename."""
    _reject_escaping_symlinks(staged)            # Sol round-3 H7
    manifest = validate_manifest(staged, name)
    artifact_id = compute_artifact_id(repo=repo, revision=revision,
                                      subdir=subdir, name=name)
    dest = Path(store_root) / name / artifact_id
    if dest.exists():
        # Idempotent re-publish: the existing copy must BE this artifact.
        # artifact_verdict is the ONE deep validator (Sol R2-1) — identity
        # mismatch or checksum failure both fail closed (spec 3.2).
        verdict = artifact_verdict(dest, name=name, repo=repo,
                                   revision=revision, subdir=subdir,
                                   artifact_id=artifact_id)
        if verdict is None:
            shutil.rmtree(staged, ignore_errors=True)
            return PublishResult(name, artifact_id, revision,
                                 manifest["version"], str(dest), manifest)
        raise StoreError(
            f"existing destination fails validation ({verdict}): {dest}",
            reason_code="corrupt_artifact")
    checksum = content_checksum(staged)
    write_metadata(staged, name=name, repo=repo, ref=ref, revision=revision,
                   subdir=subdir, artifact_id=artifact_id,
                   version=manifest["version"], checksum=checksum)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(staged, dest)
    _freeze_artifact_files(dest)
    return PublishResult(name, artifact_id, revision, manifest["version"],
                         str(dest), manifest)


def _freeze_artifact_files(root: Path) -> None:
    """Sol #7: strip write bits from a published artifact's FILES so the cached
    deep-validation's immutability assumption holds against in-place tampering
    (e.g. `echo >> skill.md` after the snapshot cached this artifact as valid).
    Directories are left writable so plugin_remove / a future gc can still
    rmtree without restoring perms. Best-effort — never fails a publish; the
    /config/plugins write guards (Sol #5) are the primary barrier.

    Sol round-3 H7: NEVER chmod through a symlink — `os.chmod(path)` follows
    symlinks, so an artifact containing `x -> /etc/passwd` would change the
    EXTERNAL target's mode. Symlinks are skipped here (and escaping symlinks are
    rejected at publish/import time by `_reject_escaping_symlinks`)."""
    import stat
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.islink(p):
                        continue
                    os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) & ~0o222)
                except OSError:
                    pass
    except OSError:
        pass


def _reject_escaping_symlinks(root: Path) -> None:
    """Sol round-3 H7: reject an artifact tree containing a symlink whose target
    escapes the artifact root (absolute path or `..`-escape). The online publish
    path is already covered by safe_extract_tar, but the offline-adopt tree-copy
    path (publish_from_tree, used by the bundled-artifact build) + the bundle import copy an
    arbitrary local tree — an escaping symlink there could expose or mutate an
    external file when the plugin is loaded. Internal (in-artifact) symlinks are
    allowed."""
    root = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root):    # followlinks=False
        for nm in list(dirnames) + list(filenames):
            p = Path(dirpath) / nm
            if not p.is_symlink():
                continue
            try:
                resolved = (Path(dirpath) / os.readlink(p)).resolve()
                resolved.relative_to(root)                # ValueError ⇒ escapes
            except (OSError, ValueError, RuntimeError) as exc:
                # Sol round-4: Path.resolve() raises RuntimeError on a symlink
                # LOOP — translate it into the same unsafe_archive contract.
                raise StoreError(
                    f"escaping or cyclic symlink in artifact: {p}",
                    reason_code="unsafe_archive") from exc


def publish(*, name: str, repo: str, ref: str, subdir: str = "",
            store_root: Path = STORE_ROOT,
            staging_root: Path = STAGING_ROOT,
            commit: str | None = None) -> PublishResult:
    """Publish an artifact from ``repo@ref``. C.2 (v0.74.0): a caller that
    already resolved the ref (the identity guards) passes ``commit`` so the
    fetch pins exactly that sha — no re-resolve window for a moving tag."""
    if commit is None:
        commit = resolve_ref(repo, ref)           # pre-mutation, may raise
    revision = f"git:{commit}"
    subdir = normalize_subdir(subdir)
    artifact_id = compute_artifact_id(repo=repo, revision=revision,
                                      subdir=subdir, name=name)
    staged = Path(staging_root) / artifact_id
    if staged.exists():
        shutil.rmtree(staged)
    staged.parent.mkdir(parents=True, exist_ok=True)
    try:
        fetch_commit_tree(repo, commit, subdir, staged)
        return _stage_and_swap(name=name, repo=repo, ref=ref,
                               revision=revision, subdir=subdir,
                               staged=staged, store_root=store_root)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise


def publish_from_tree(*, name: str, repo: str, ref: str, revision: str,
                      subdir: str, src_root: Path,
                      store_root: Path = STORE_ROOT,
                      staging_root: Path = STAGING_ROOT,
                      exclude_git: bool = True) -> PublishResult:
    subdir = normalize_subdir(subdir)
    artifact_id = compute_artifact_id(repo=repo, revision=revision,
                                      subdir=subdir, name=name)
    staged = Path(staging_root) / artifact_id
    if staged.exists():
        shutil.rmtree(staged)
    staged.parent.mkdir(parents=True, exist_ok=True)
    try:
        ignore = shutil.ignore_patterns(".git") if exclude_git else None
        shutil.copytree(src_root, staged, symlinks=True, ignore=ignore)
        # Drop a stale metadata file if the source was itself an artifact.
        meta = staged / METADATA_FILENAME
        if meta.exists():
            meta.unlink()
        return _stage_and_swap(name=name, repo=repo, ref=ref,
                               revision=revision, subdir=subdir,
                               staged=staged, store_root=store_root)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise


def import_bundle(bundle_root: Path, store_root: Path = STORE_ROOT) -> list:
    """Boot import of image-baked artifacts (§3.6). Idempotent, checksum-
    verified, fail-closed on an existing-but-corrupt store copy."""
    from plugin_registry import PluginIssue
    issues: list[PluginIssue] = []
    bundle_root = Path(bundle_root)
    if not bundle_root.is_dir():
        return issues
    for name_dir in sorted(p for p in bundle_root.iterdir() if p.is_dir()):
        for art_dir in sorted(p for p in name_dir.iterdir() if p.is_dir()):
            dest = Path(store_root) / name_dir.name / art_dir.name
            if dest.exists():
                if not validate_artifact(dest):
                    issues.append(PluginIssue(
                        name=name_dir.name, target=None, stage="import",
                        reason_code="corrupt_artifact",
                        artifact_id=art_dir.name))
                continue
            tmp = dest.parent / f".import-{art_dir.name}"
            try:
                if tmp.exists():
                    shutil.rmtree(tmp)
                tmp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(art_dir, tmp, symlinks=True)
                _reject_escaping_symlinks(tmp)       # Sol round-3 H7
                if not validate_artifact(tmp):
                    raise StoreError("bundle copy failed checksum",
                                     reason_code="corrupt_artifact")
                os.rename(tmp, dest)
                _freeze_artifact_files(dest)         # Sol round-3 H7: freeze imports too
            except (OSError, StoreError) as exc:
                shutil.rmtree(tmp, ignore_errors=True)
                code = getattr(exc, "reason_code", "import_failed")
                issues.append(PluginIssue(
                    name=name_dir.name, target=None, stage="import",
                    reason_code=code, artifact_id=art_dir.name))
    return issues


def gc_sweep(*, store_root: Path = STORE_ROOT, referenced: set[str],
             min_age_days: int = 7, enabled: bool = False) -> list[str]:
    """§3.12 conservative GC. SHIPS DISABLED: no production caller passes
    enabled=True this release; with enabled=False only reports candidates."""
    import time
    cutoff = time.time() - min_age_days * 86400
    candidates: list[str] = []
    root = Path(store_root)
    if not root.is_dir():
        return candidates
    for name_dir in root.iterdir():
        if not name_dir.is_dir():
            continue
        for art_dir in name_dir.iterdir():
            if not art_dir.is_dir() or art_dir.name in referenced:
                continue
            try:
                if art_dir.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            candidates.append(art_dir.name)
            if enabled:
                shutil.rmtree(art_dir, ignore_errors=True)
    return sorted(candidates)
