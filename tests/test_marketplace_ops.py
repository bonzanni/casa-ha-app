"""Low-level marketplace file I/O (mutations are via user marketplace only)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from marketplace_ops import (
    USER_MARKETPLACE_PATH,
    MarketplaceError,
    add_plugin_entry,
    list_plugin_entries,
    load_user_marketplace,
    remove_plugin_entry,
    update_plugin_entry,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def user_mkt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "marketplace" / ".claude-plugin" / "marketplace.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "name": "casa-plugins", "owner": {"name": "t"}, "plugins": [],
    }), encoding="utf-8")
    monkeypatch.setattr("marketplace_ops.USER_MARKETPLACE_PATH", target)
    return target


def test_add_appends(user_mkt: Path) -> None:
    add_plugin_entry({"name": "a", "source": {"source": "github", "repo": "u/a"},
                      "description": "x", "version": "0.1.0"})
    data = json.loads(user_mkt.read_text())
    assert len(data["plugins"]) == 1
    assert data["plugins"][0]["name"] == "a"


def test_add_duplicate_raises(user_mkt: Path) -> None:
    add_plugin_entry({"name": "a", "source": {"source": "github", "repo": "u/a"},
                      "description": "x", "version": "0.1.0"})
    with pytest.raises(MarketplaceError, match="already exists"):
        add_plugin_entry({"name": "a", "source": {"source": "github", "repo": "u/a"},
                          "description": "x", "version": "0.1.0"})


def test_remove_happy(user_mkt: Path) -> None:
    add_plugin_entry({"name": "a", "source": {"source": "github", "repo": "u/a"},
                      "description": "x", "version": "0.1.0"})
    removed = remove_plugin_entry("a")
    assert removed is True
    data = json.loads(user_mkt.read_text())
    assert data["plugins"] == []


def test_remove_nonexistent_raises(user_mkt: Path) -> None:
    with pytest.raises(MarketplaceError, match="not found"):
        remove_plugin_entry("ghost")


def test_update_happy(user_mkt: Path) -> None:
    add_plugin_entry({"name": "a", "source": {"source": "github", "repo": "u/a", "sha": "old"},
                      "description": "x", "version": "0.1.0"})
    update_plugin_entry("a", new_ref="new")
    data = json.loads(user_mkt.read_text())
    assert data["plugins"][0]["source"]["sha"] == "new"


def test_list_returns_entries(user_mkt: Path) -> None:
    assert list_plugin_entries() == []
    add_plugin_entry({"name": "a", "source": {"source": "github", "repo": "u/a"},
                      "description": "x", "version": "0.1.0"})
    entries = list_plugin_entries()
    assert len(entries) == 1 and entries[0]["name"] == "a"


def test_load_rejects_malformed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "bad.json"
    target.write_text("not json", encoding="utf-8")
    monkeypatch.setattr("marketplace_ops.USER_MARKETPLACE_PATH", target)
    with pytest.raises(MarketplaceError):
        load_user_marketplace()


def test_write_is_atomic_crash_keeps_original(user_mkt: Path, monkeypatch) -> None:
    """L15: a crash BETWEEN the temp write and os.replace must leave the prior
    marketplace.json intact — a truncated file bricks every marketplace op."""
    import atomic_io

    add_plugin_entry({"name": "a", "source": {"source": "github", "repo": "u/a"},
                      "description": "x", "version": "0.1.0"})
    before = user_mkt.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(atomic_io.os, "replace", boom)
    with pytest.raises(RuntimeError):
        add_plugin_entry({"name": "b", "source": {"source": "github", "repo": "u/b"},
                          "description": "y", "version": "0.2.0"})

    # Original intact, not truncated; still loads and has only 'a'.
    assert user_mkt.read_text(encoding="utf-8") == before
    data = json.loads(user_mkt.read_text(encoding="utf-8"))
    assert [p["name"] for p in data["plugins"]] == ["a"]
    # No orphaned temp sidecar in the marketplace dir.
    import os as _os
    leftovers = [f for f in _os.listdir(user_mkt.parent) if f != "marketplace.json"]
    assert leftovers == []


def _write_mkt(path: Path, plugins: list) -> None:
    path.write_text(json.dumps({
        "name": "casa-plugins", "owner": {"name": "t"}, "plugins": plugins,
    }), encoding="utf-8")


def test_update_string_source_raises_marketplace_error(user_mkt: Path) -> None:
    """L66/L16: a hand-edited entry using CC's legal string-source form
    must raise MarketplaceError, not TypeError, and must not partially
    write the file."""
    _write_mkt(user_mkt, [{"name": "x", "source": "./plugins/x"}])
    with pytest.raises(MarketplaceError, match="source"):
        update_plugin_entry("x", new_ref="abc123")
    # file unchanged — no partial write
    assert json.loads(user_mkt.read_text())["plugins"][0]["source"] == "./plugins/x"


def test_entry_missing_name_raises_marketplace_error(user_mkt: Path) -> None:
    """L66/L16: an entry lacking 'name' must raise MarketplaceError (not
    KeyError) from add/remove/update alike."""
    _write_mkt(user_mkt, [{"description": "hand-added, no name"}])
    with pytest.raises(MarketplaceError, match="name"):
        add_plugin_entry({"name": "y", "source": {"source": "github", "repo": "u/y"}, "description": "d"})
    with pytest.raises(MarketplaceError, match="name"):
        remove_plugin_entry("y")
    with pytest.raises(MarketplaceError, match="name"):
        update_plugin_entry("y", new_ref="abc")
