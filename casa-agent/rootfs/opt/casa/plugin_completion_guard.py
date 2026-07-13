"""A.2 enforcement seam (spec 2026-07-13, v0.74.0): validate a
plugin-developer completion's ``casa_plugin_repo`` artifacts at the
``emit_completion`` boundary — the mechanical gate that makes forgetting the
release ritual impossible (doctrine alone cannot).

Every ``casa_plugin_repo`` artifact on an ok-completion must carry a verified
release identity — ALL THREE handed-off fields are enforced: ``ref`` is a
``vX.Y.Z`` tag whose remote tag object is ANNOTATED and peels to exactly
``revision``; ``ref == "v" + <REMOTE plugin.json.version>`` at that commit;
and the artifact's own ``version`` equals the remote manifest's (the producer
cannot self-certify any of them). Re-resolution goes through the SAME
resolver ``plugin_update`` uses (plugin_store.resolve_ref — private-repo auth
included), so producer-verify and configurator-pin agree by construction.

Blocking (gh subprocess round trips) — call via ``asyncio.to_thread``.
"""
from __future__ import annotations

import json
import logging
import re

import plugin_store

logger = logging.getLogger(__name__)

_REPO_URL_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)?"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def repo_from_url(repo_url) -> str | None:
    """Normalize a completion artifact's repo_url to ``owner/name``. Accepts
    https://github.com/o/r(.git), git@github.com:o/r(.git), bare o/r.
    Non-GitHub hosts -> None (the resolver is GitHub-only)."""
    if not isinstance(repo_url, str) or not repo_url.strip():
        return None
    m = _REPO_URL_RE.match(repo_url.strip())
    return f"{m.group('owner')}/{m.group('name')}" if m else None


def _fail(index, reason_code: str, message: str) -> dict:
    return {"index": index, "reason_code": reason_code, "message": message}


def validate_completion_artifacts(artifacts: list) -> list[dict]:
    """Validate EVERY casa_plugin_repo artifact (spec A.2 — never just the
    first). [] when all pass, else one failure dict per problem. A transient
    GitHub failure surfaces as resolve_unavailable — the producer retries
    emit_completion once the lag clears."""
    repo_arts = [(i, a) for i, a in enumerate(artifacts or [])
                 if isinstance(a, dict) and a.get("kind") == "casa_plugin_repo"]
    if not repo_arts:
        return [_fail(None, "no_plugin_repo_artifact",
                      "an ok-completion from plugin-developer must carry at "
                      "least one casa_plugin_repo artifact (see "
                      "doctrine/casa-conventions.md — completion schema)")]
    failures: list[dict] = []
    for i, art in repo_arts:
        failures.extend(_validate_one(i, art))
    return failures


def _validate_one(index: int, art: dict) -> list[dict]:
    repo = repo_from_url(art.get("repo_url"))
    if repo is None:
        return [_fail(index, "bad_repo_url",
                      f"repo_url {art.get('repo_url')!r} is not a GitHub "
                      "repo URL")]
    ref = art.get("ref")
    if not isinstance(ref, str) or not plugin_store.RELEASE_TAG_RE.match(ref):
        return [_fail(index, "ref_not_release_tag",
                      f"ref must be the release tag 'vX.Y.Z' (got {ref!r}) — "
                      "push an annotated tag named 'v' + plugin.json.version "
                      "and hand THAT off")]
    revision = plugin_store.normalize_revision(art.get("revision"))
    if revision is None:
        return [_fail(index, "bad_revision",
                      f"revision must be a 40-hex commit sha (got "
                      f"{art.get('revision')!r})")]
    try:
        # 1. The tag object must exist and be ANNOTATED (a lightweight tag's
        #    ref points straight at a commit — verified live 2026-07-13:
        #    object.type is "tag" for annotated, "commit" for lightweight).
        status, _headers, body = plugin_store.gh_api_probe(
            f"repos/{repo}/git/ref/tags/{ref}")
        if status != 200:
            if status in (404, 422):
                return [_fail(index, "tag_missing",
                              f"tag {ref} not found on {repo} (HTTP "
                              f"{status}) — was it pushed?")]
            return [_fail(index, "resolve_unavailable",
                          f"GitHub tag lookup failed (HTTP {status}); "
                          "retry emit_completion shortly")]
        try:
            obj_type = (json.loads(body or "{}").get("object") or {}).get("type")
        except ValueError:
            return [_fail(index, "resolve_unavailable",
                          "unparseable tag-ref response; retry shortly")]
        if obj_type != "tag":
            return [_fail(index, "tag_not_annotated",
                          f"tag {ref} on {repo} is lightweight — release "
                          "tags must be annotated (git tag -a)")]
        # 2. Peel on the remote == the completion's revision. The commits
        #    API auto-peels annotated tags — the SAME call plugin_update
        #    pins by (§C.1 resolver, private-repo auth included).
        try:
            resolved = plugin_store.resolve_ref(repo, ref)
        except plugin_store.RefNotFound:
            return [_fail(index, "tag_missing",
                          f"{repo}@{ref} does not resolve to a commit — the "
                          "annotated tag exists but cannot be peeled; was "
                          "the push complete?")]
        if resolved != revision:
            return [_fail(index, "revision_mismatch",
                          f"tag {ref} peels to {resolved} on the remote but "
                          f"the completion claims revision {revision} — did "
                          "the tag move after the build?")]
        # 3. ref == "v" + the REMOTE manifest's version at that exact commit
        #    (the producer cannot self-certify).
        status, _headers, body = plugin_store.gh_api_probe(
            f"repos/{repo}/contents/.claude-plugin/plugin.json?ref={resolved}",
            accept="application/vnd.github.raw+json")
        if status != 200:
            return [_fail(index, "manifest_unavailable",
                          f".claude-plugin/plugin.json not readable at "
                          f"{repo}@{resolved[:12]} (HTTP {status})")]
        try:
            remote_version = json.loads(body or "").get("version")
        except ValueError:
            return [_fail(index, "manifest_invalid",
                          "remote plugin.json is not valid JSON")]
        if ref != f"v{remote_version}":
            return [_fail(index, "tag_version_mismatch",
                          f"ref {ref} != v{remote_version} (the REMOTE "
                          "plugin.json.version at the resolved commit) — "
                          "bump plugin.json and re-tag")]
        # 4. r2-B5: the artifact's OWN version field must match the remote —
        #    all three handed-off identity fields are enforced.
        if art.get("version") != remote_version:
            return [_fail(index, "version_mismatch",
                          f"completion version {art.get('version')!r} != "
                          f"remote plugin.json version {remote_version!r}")]
    except plugin_store.ResolveAuthFailed as exc:
        return [_fail(index, "resolve_auth_failed", str(exc))]
    except plugin_store.StoreError as exc:
        # ResolveUnavailable + any other transient taxonomy: retryable —
        # reject so the producer can retry after the lag clears.
        return [_fail(index, getattr(exc, "reason_code", "resolve_unavailable"),
                      f"{exc} — retry emit_completion shortly")]
    return []
