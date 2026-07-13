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
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from plugin_registry import STORE_ROOT, compute_artifact_id, normalize_subdir

logger = logging.getLogger(__name__)

STAGING_ROOT = Path("/config/plugins/.staging")
METADATA_FILENAME = ".casa-artifact.json"


class StoreError(Exception):
    reason_code = "store_error"

    def __init__(self, message: str, *, reason_code: str | None = None):
        super().__init__(message)
        if reason_code is not None:
            self.reason_code = reason_code


class RefNotFound(StoreError):
    reason_code = "ref_not_found"


class ResolveUnavailable(StoreError):
    reason_code = "resolve_unavailable"


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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("plugin .mcp.json unreadable (%s): %s", path, exc)
        return {}, True
    if not isinstance(data, dict):
        logger.debug("plugin .mcp.json not an object (%s)", path)
        return {}, True
    def _runnable(cfg) -> bool:
        # A runnable server declares a non-empty command (stdio) OR url (http/sse).
        return isinstance(cfg, dict) and (
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
            logger.debug("plugin .mcp.json mcpServers not a mapping (%s)", path)
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


def resolve_ref(repo: str, ref: str, *, timeout: float = 20.0) -> str:
    """Resolve a ref to a 40-hex commit via the GitHub commits API (gh CLI).
    HTTP 404 => RefNotFound (hard, pre-mutation). Anything else network/auth/
    timeout/tooling => ResolveUnavailable (retryable). NEVER conflated."""
    argv = ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ResolveUnavailable(f"gh api timeout for {repo}@{ref}") from exc
    except (FileNotFoundError, OSError) as exc:
        raise ResolveUnavailable(f"gh unavailable: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        if "HTTP 404" in err:
            raise RefNotFound(f"{repo}@{ref} not found (HTTP 404)")
        raise ResolveUnavailable(f"gh api failed for {repo}@{ref}: "
                                 f"{err.strip()[:200]}")
    out = proc.stdout.strip()
    # `--jq .sha` prints the bare sha; tolerate full-JSON output too.
    if out.startswith("{"):
        try:
            out = json.loads(out).get("sha", "")
        except ValueError:
            out = ""
    sha = out.strip().strip('"')
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha.lower()):
        raise ResolveUnavailable(f"unparseable sha for {repo}@{ref}: {out!r}")
    return sha.lower()


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
            reason_code="name_mismatch")
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
    paths (publish_from_tree / publish_legacy_tree) and the bundle import copy an
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
            staging_root: Path = STAGING_ROOT) -> PublishResult:
    commit = resolve_ref(repo, ref)               # pre-mutation, may raise
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


def publish_legacy_tree(*, name: str, repo: str, ref: str, subdir: str,
                        src_root: Path, store_root: Path = STORE_ROOT,
                        staging_root: Path = STAGING_ROOT) -> PublishResult:
    """Offline-adopt a legacy checkout with a content-DERIVED revision
    (``legacy-content:<checksum>``). The checksum is taken over the CANONICAL
    STAGED tree (post ``.git`` / stale-metadata exclusion), so the recorded
    identity matches exactly what lands in the store (Sol F9). Used only by the
    one-time migration's offline-adopt path (§3.7)."""
    subdir = normalize_subdir(subdir)
    Path(staging_root).mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(dir=str(staging_root), prefix=".legacy-"))
    staged = holder / "artifact"
    try:
        shutil.copytree(src_root, staged, symlinks=True,
                        ignore=shutil.ignore_patterns(".git"))
        meta = staged / METADATA_FILENAME
        if meta.exists():
            meta.unlink()
        checksum = content_checksum(staged)
        revision = f"legacy-content:{checksum}"
        return _stage_and_swap(name=name, repo=repo, ref=ref,
                               revision=revision, subdir=subdir,
                               staged=staged, store_root=store_root)
    finally:
        shutil.rmtree(holder, ignore_errors=True)


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
