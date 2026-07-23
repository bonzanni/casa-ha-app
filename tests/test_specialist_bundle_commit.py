"""Task 10 — journaled bundle transaction: owned-set sidecar, atomic owned
registry swap, and the commit/upgrade/rollback/uninstall integration.

Checkpoint 2a covers the two self-contained primitives (sidecar triple +
apply_owned_swap); the lifecycle-integration slices (2b-2d) follow."""
from __future__ import annotations

from pathlib import Path

import pytest

import plugin_registry
from plugin_registry import apply_owned_swap, compute_artifact_id, scoped_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _owned_entry(slug: str, manifest_name: str, *, repo: str = "acme/mtg",
                 revision: str = "git:" + "a" * 40, subdir: str = "plugins/mtg",
                 version: str = "1.0.0") -> dict:
    name = scoped_name(slug, manifest_name)
    return {
        "name": name,
        "owner": f"specialist:{slug}",
        "manifest_name": manifest_name,
        "targets": [f"specialist:{slug}"],
        "version": version,
        "source": {"type": "github", "repo": repo,
                   "ref": "v1", "revision": revision, "subdir": subdir},
        "artifact_id": compute_artifact_id(
            repo=repo, revision=revision, subdir=subdir, name=name),
    }


def _unowned_entry(name: str = "weather", *, repo: str = "acme/weather") -> dict:
    revision = "git:" + "b" * 40
    return {
        "name": name,
        "targets": ["resident:assistant"],
        "version": "2.0.0",
        "source": {"type": "github", "repo": repo,
                   "ref": "v2", "revision": revision, "subdir": ""},
        "artifact_id": compute_artifact_id(
            repo=repo, revision=revision, subdir="", name=name),
    }


def _write_registry(path: Path, entries: list[dict]) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1, "seeded_defaults": [], "plugins": entries,
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# 2a — apply_owned_swap
# ---------------------------------------------------------------------------

def test_apply_owned_swap_install_adds_owned_entries(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [_unowned_entry()])
    entry = _owned_entry("mtg", "mtg")

    before, data = apply_owned_swap(slug="mtg", new_entries=[entry], registry_path=reg)

    assert before == []                 # nothing owned before
    names = {e["name"] for e in plugin_registry.load_registry(reg).entries}
    assert names == {"weather", "mtg.mtg"}
    # owner + manifest_name + targets survived validation
    owned = plugin_registry.owned_entries_for("mtg", plugin_registry.load_registry(reg))
    assert len(owned) == 1
    assert owned[0]["manifest_name"] == "mtg"
    assert owned[0]["targets"] == ["specialist:mtg"]


def test_apply_owned_swap_replaces_prior_owned_set_and_returns_before(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    old = _owned_entry("mtg", "mtg")
    _write_registry(reg, [_unowned_entry(), old])
    new = _owned_entry("mtg", "mtg", version="2.0.0", revision="git:" + "c" * 40)

    before, _ = apply_owned_swap(slug="mtg", new_entries=[new], registry_path=reg)

    assert [e["name"] for e in before] == ["mtg.mtg"]
    assert before[0]["version"] == "1.0.0"
    owned = plugin_registry.owned_entries_for("mtg", plugin_registry.load_registry(reg))
    assert len(owned) == 1 and owned[0]["version"] == "2.0.0"


def test_apply_owned_swap_uninstall_removes_owned_only(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [_unowned_entry(), _owned_entry("mtg", "mtg")])

    before, _ = apply_owned_swap(slug="mtg", new_entries=[], registry_path=reg)

    assert [e["name"] for e in before] == ["mtg.mtg"]
    names = {e["name"] for e in plugin_registry.load_registry(reg).entries}
    assert names == {"weather"}         # unowned survivor untouched


def test_apply_owned_swap_leaves_other_specialists_entries_alone(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [_owned_entry("mtg", "mtg"), _owned_entry("finance", "ledger")])

    apply_owned_swap(slug="mtg", new_entries=[], registry_path=reg)

    names = {e["name"] for e in plugin_registry.load_registry(reg).entries}
    assert names == {"finance.ledger"}


def test_apply_owned_swap_refuses_a_malformed_new_entry(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [])
    bad = _owned_entry("mtg", "mtg")
    bad["artifact_id"] = "deadbeef"     # identity mismatch -> entry_invalid

    with pytest.raises(ValueError, match="owned_swap_invalid"):
        apply_owned_swap(slug="mtg", new_entries=[bad], registry_path=reg)
    # registry file untouched (never saved on refusal)
    assert plugin_registry.load_registry(reg).entries == []


def test_apply_owned_swap_refuses_a_manifest_name_collision(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [])
    a = _owned_entry("mtg", "mtg")
    b = _owned_entry("mtg", "mtg", revision="git:" + "d" * 40)  # same scoped name
    with pytest.raises(ValueError, match="owned_swap_invalid"):
        apply_owned_swap(slug="mtg", new_entries=[a, b], registry_path=reg)


# ---------------------------------------------------------------------------
# 2a — owned-plugins sidecar triple
# ---------------------------------------------------------------------------

def _doc(plugins: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "component_source": {"repo": "acme/mtg-specialist", "ref": "v0.2.0",
                             "revision": "git:" + "a" * 40, "subdir": ""},
        "plugins": plugins if plugins is not None else [
            {"name": "mtg.mtg", "manifest_name": "mtg", "version": "1.0.0",
             "artifact_id": "x" * 64, "digest": "sha256:" + "y" * 64,
             "source": {"type": "github", "repo": "acme/mtg-specialist",
                        "ref": "v0.2.0", "revision": "git:" + "a" * 40,
                        "subdir": "plugins/mtg"}},
        ],
    }


def test_owned_plugins_sidecar_roundtrip(tmp_path: Path) -> None:
    from personality_binding import (
        owned_plugins_path, read_owned_plugins, write_owned_plugins,
    )
    p = owned_plugins_path(tmp_path)
    assert read_owned_plugins(p) is None
    write_owned_plugins(p, _doc())
    loaded = read_owned_plugins(p)
    assert loaded is not None
    assert loaded["component_source"]["repo"] == "acme/mtg-specialist"
    assert loaded["plugins"][0]["name"] == "mtg.mtg"


def test_owned_plugins_supports_plugin_less_component(tmp_path: Path) -> None:
    from personality_binding import owned_plugins_path, read_owned_plugins, write_owned_plugins
    p = owned_plugins_path(tmp_path)
    write_owned_plugins(p, _doc(plugins=[]))
    loaded = read_owned_plugins(p)
    assert loaded["plugins"] == []
    assert loaded["component_source"]["repo"]      # provenance still present


def test_commit_owned_plugins_rotates_desired_to_active_and_prior(tmp_path: Path) -> None:
    from personality_binding import (
        InstanceDir, owned_plugins_path, owned_plugins_prior_path,
        owned_plugins_desired_path, read_owned_plugins, write_owned_plugins,
    )
    d = InstanceDir(tmp_path)
    # generation 1 already active
    write_owned_plugins(owned_plugins_path(tmp_path), _doc())
    gen1 = read_owned_plugins(owned_plugins_path(tmp_path))
    # stage generation 2 as desired
    gen2 = _doc(plugins=[])
    d.stage_desired_owned_plugins(gen2)
    assert read_owned_plugins(owned_plugins_desired_path(tmp_path)) == gen2

    d.commit_owned_plugins_desired_to_active()

    assert read_owned_plugins(owned_plugins_path(tmp_path)) == gen2       # new active
    assert read_owned_plugins(owned_plugins_prior_path(tmp_path)) == gen1  # old -> prior
    assert not owned_plugins_desired_path(tmp_path).exists()               # consumed


def test_commit_owned_plugins_is_noop_without_a_staged_desired(tmp_path: Path) -> None:
    from personality_binding import InstanceDir, owned_plugins_path, read_owned_plugins, write_owned_plugins
    d = InstanceDir(tmp_path)
    write_owned_plugins(owned_plugins_path(tmp_path), _doc())
    d.commit_owned_plugins_desired_to_active()     # no desired staged
    assert read_owned_plugins(owned_plugins_path(tmp_path)) is not None
