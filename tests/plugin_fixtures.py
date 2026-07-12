"""Shared fixtures for unified-plugin-architecture tests: build store
artifacts (with identity-matching metadata) + registry files + entries.

Metadata identity MUST match the registry entry's (name/repo/revision/
subdir/artifact_id) or plugin_store.artifact_verdict → artifact_invalid.
"""
from __future__ import annotations

import json
from pathlib import Path

from plugin_registry import compute_artifact_id
from plugin_store import content_checksum, write_metadata


def mk_artifact(store: Path, name: str, artifact_id: str,
                version: str = "1.0.0", manifest_name: str | None = None,
                revision: str = "git:" + "a" * 40, subdir: str = "",
                mcp_servers: dict | None = None) -> Path:
    root = Path(store) / name / artifact_id
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": manifest_name or name, "version": version}),
        encoding="utf-8")
    if mcp_servers is not None:
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": mcp_servers}), encoding="utf-8")
    write_metadata(root, name=name, repo="o/r", ref="v1",
                   revision=revision, subdir=subdir,
                   artifact_id=artifact_id, version=version,
                   checksum=content_checksum(root))
    return root


def entry(name: str, targets: list[str], revision: str = "git:" + "a" * 40,
          subdir: str = "", version: str = "1.0.0") -> dict:
    return {
        "name": name,
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": revision, "subdir": subdir},
        "artifact_id": compute_artifact_id(repo="o/r", revision=revision,
                                           subdir=subdir, name=name),
        "version": version,
        "targets": targets,
    }


def mk_registry(tmp_path: Path, entries: list[dict]) -> Path:
    p = Path(tmp_path) / "registry.json"
    p.write_text(json.dumps({"schema_version": 1, "plugins": entries}),
                 encoding="utf-8")
    return p
