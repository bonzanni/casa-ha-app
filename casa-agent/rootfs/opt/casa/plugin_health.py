"""Plugin health — durable report + operator notification (spec §3.10).

Every boot and every mutation regenerates /data/plugin-health.json. Issue
"fingerprints" hash the STRUCTURED fields (name, target, stage, reason_code,
artifact_id) — never free-form reason text — so a wording change never
re-alerts. A post-boot Telegram DM fires (via the deterministic bus, like
notify_config_sync) when the report contains NEW fingerprints; an issue
disappearing from the report clears its fingerprint. While the report holds
unresolved blocking issues, the affected resident prepends a one-line notice
to its first user-visible turn (first_contact_notice).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HEALTH_PATH = Path("/data/plugin-health.json")


def fingerprint(issue) -> str:
    """SHA-256 over the STRUCTURED issue fields only (§3.10). A PluginIssue or
    an already-serialized issue dict both work."""
    def _get(field: str):
        if isinstance(issue, dict):
            return issue.get(field)
        return getattr(issue, field, None)
    body = "\x00".join([
        str(_get("name") or ""),
        str(_get("target") or ""),
        str(_get("stage") or ""),
        str(_get("reason_code") or ""),
        str(_get("artifact_id") or ""),
    ])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _issue_dict(issue) -> dict:
    return {
        "name": getattr(issue, "name", None),
        "target": getattr(issue, "target", None),
        "stage": getattr(issue, "stage", None),
        "reason_code": getattr(issue, "reason_code", None),
        "artifact_id": getattr(issue, "artifact_id", None),
        "fingerprint": fingerprint(issue),
    }


def _atomic_write(path: Path, report: dict) -> None:
    from atomic_io import atomic_write_text
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(Path(path),
                      json.dumps(report, indent=2, sort_keys=True) + "\n")


def load_report(path: Path = HEALTH_PATH) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_report(*, issues: list, warnings: list,
                 path: Path = HEALTH_PATH) -> dict:
    """Regenerate the health report atomically. `notified_fingerprints` are
    carried forward from the previous report but pruned to fingerprints still
    present (a resolved issue clears its fingerprint). Returns the report."""
    prev = load_report(path) or {}
    prev_notified = set(prev.get("notified_fingerprints") or [])
    issue_dicts = [_issue_dict(i) for i in issues]
    warning_dicts = [_issue_dict(w) for w in warnings]
    current_fps = {d["fingerprint"] for d in issue_dicts}
    current_fps |= {d["fingerprint"] for d in warning_dicts}
    report = {
        "schema_version": 1,
        "issues": issue_dicts,
        "warnings": warning_dicts,
        "notified_fingerprints": sorted(prev_notified & current_fps),
    }
    _atomic_write(path, report)
    return report


def new_fingerprints(report: dict) -> list[str]:
    """Issue fingerprints not yet notified (order-preserving, deduped)."""
    notified = set(report.get("notified_fingerprints") or [])
    seen: set[str] = set()
    out: list[str] = []
    for d in report.get("issues", []):
        fp = d.get("fingerprint")
        if fp and fp not in notified and fp not in seen:
            seen.add(fp)
            out.append(fp)
    return out


def mark_notified(fps: list[str], path: Path = HEALTH_PATH) -> None:
    report = load_report(path)
    if report is None:
        return
    notified = list(report.get("notified_fingerprints") or [])
    for fp in fps:
        if fp not in notified:
            notified.append(fp)
    report["notified_fingerprints"] = notified
    _atomic_write(path, report)


def first_contact_notice(role: str, path: Path = HEALTH_PATH) -> str | None:
    """One-line notice for the affected resident's first user-visible turn if
    the report holds a blocking issue targeting this role (or registry-wide,
    target=None); else None (§3.10)."""
    report = load_report(path)
    if not report:
        return None
    ok_targets = {f"resident:{role}", f"specialist:{role}", None}
    matched = [d for d in report.get("issues", [])
               if d.get("target") in ok_targets]
    if not matched:
        return None
    parts = [f"{d.get('name')} ({d.get('reason_code')})" for d in matched[:2]]
    body = ", ".join(parts)
    if len(matched) > 2:
        body += f" +{len(matched) - 2} more"
    return f"⚠️ Plugin degraded: {body} — an operator has been notified."
