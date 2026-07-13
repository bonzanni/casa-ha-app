#!/usr/bin/env python3
"""Deterministic build-time materialization of bundled plugin artifacts
(spec 3.6). Fetches each default-registry entry's EXACT pinned commit
(never resolves a tag/branch at build time), publishes into --out, and
VERIFIES the checked-in artifact_id and version match — a mismatch fails
the image build."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # /opt/casa

from plugin_registry import compute_artifact_id  # noqa: E402
from plugin_store import fetch_commit_tree, publish_from_tree  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    doc = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    out = Path(args.out)
    staging = out.parent / ".bundle-staging"
    for entry in doc["plugins"]:
        src, name = entry["source"], entry["name"]
        revision = src["revision"]
        assert revision.startswith("git:"), f"{name}: non-git bundled pin"
        commit = revision[len("git:"):]
        expected_id = compute_artifact_id(
            repo=src["repo"], revision=revision,
            subdir=src.get("subdir", ""), name=name)
        assert entry["artifact_id"] == expected_id, (
            f"{name}: checked-in artifact_id {entry['artifact_id']} != "
            f"computed {expected_id}")
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td) / "tree"
            fetch_commit_tree(src["repo"], commit, src.get("subdir", ""), tree)
            res = publish_from_tree(
                name=name, repo=src["repo"], ref=src["ref"],
                revision=revision, subdir=src.get("subdir", ""),
                src_root=tree, store_root=out, staging_root=staging)
        assert res.artifact_id == expected_id
        assert res.version == entry["version"], (
            f"{name}: manifest version {res.version!r} != checked-in "
            f"{entry['version']!r}")
        # Sol R2-5: pin the REAL fetched .mcp.json server keys so the executor
        # allow-list (mcp__plugin_context7_context7) provably matches the
        # materialized artifact — not a fixture's claim. Use the SAME server
        # detection grants use, so it also understands context7's top-level
        # (no-mcpServers-wrapper) .mcp.json shape.
        if name == "context7":
            from plugin_store import mcp_servers_map
            keys = sorted(mcp_servers_map(Path(res.path) / ".mcp.json").keys())
            assert keys == ["context7"], (
                f"context7 server keys drifted: {keys} — update "
                f"plugin-developer definition.yaml grants to match")
        print(f"bundled: {name} {res.version} -> {res.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
