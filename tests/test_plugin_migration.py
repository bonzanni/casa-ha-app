"""§3.7 one-time migration: installed-state-driven, offline-safe, disablement-
wins, executor-plugins.yaml-authoritative, report-then-sentinel, guarded on an
unreadable registry. FR8."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import plugin_migration as pm
from plugin_store import PublishResult

pytestmark = pytest.mark.unit


# --- parsers ----------------------------------------------------------------

def test_cli_row_id_parsing():
    rows = [{"id": "lesina-invoice@casa-plugins", "installPath": "/a",
             "scope": "project", "enabled": True},
            {"id": "superpowers@casa-plugins-defaults", "installPath": "/b",
             "scope": "user", "enabled": False}]
    out = pm._parse_installed_rows(rows)
    assert out["lesina-invoice"]["installPaths"] == {"/a"}
    assert out["lesina-invoice"]["enabled"] is True
    assert out["superpowers"]["scopes"] == {"user"}


def test_fallback_file_parsing():
    doc = {"lesina-invoice@casa-plugins": {"installPath": "/a", "scope": "project"}}
    out = pm._parse_installed_rows(doc)
    assert "lesina-invoice" in out
    assert out["lesina-invoice"]["installPaths"] == {"/a"}


def test_installed_plugins_v2_nested_schema():
    """Sol #2: the REAL installed_plugins.json (CC 2.1.x) is
    {"version":2,"plugins":{"<name>@<mkt>":[<records>]}} — the map is under
    "plugins" and each value is a LIST. The old parser saw only "version"/
    "plugins" keys and extracted zero plugins."""
    doc = {"version": 2, "plugins": {
        "lesina-invoice@casa-plugins": [
            {"installPath": "/a", "scope": "user", "projectPath": None}],
        "superpowers@casa-plugins-defaults": [
            {"installPath": "/b", "scope": "user"}]}}
    out = pm._parse_installed_rows(doc)
    assert out["lesina-invoice"]["installPaths"] == {"/a"}
    assert out["superpowers"]["installPaths"] == {"/b"}
    # The envelope keys must NOT be mistaken for plugins.
    assert "version" not in out and "plugins" not in out


def test_installed_plugins_v2_multiple_records_flattened():
    """Sol #2: several install records under one key (user + project scope) all
    contribute their installPaths."""
    doc = {"version": 2, "plugins": {"p@m": [
        {"installPath": "/u", "scope": "user"},
        {"installPath": "/pr", "scope": "project"}]}}
    out = pm._parse_installed_rows(doc)
    assert out["p"]["installPaths"] == {"/u", "/pr"}
    assert out["p"]["scopes"] == {"user", "project"}


def test_atomic_touch_creates_sentinel(tmp_path):
    s = tmp_path / "sub" / ".migration-done"
    pm._atomic_touch(s)
    assert s.is_file()


# --- run_migration harness --------------------------------------------------

def _pr(name, revision="git:" + "d" * 40, version="1.2.0"):
    return PublishResult(name=name, artifact_id="a" * 64, revision=revision,
                         version=version, path=f"/store/{name}/x",
                         manifest={"name": name, "version": version})


def _dirs(tmp_path):
    d = {k: tmp_path / k for k in
         ("cc_home", "config", "data", "defaults", "agents", "homes",
          "store", "staging", "plugins")}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _default_registry(defaults_dir, names):
    doc = {"schema_version": 1, "plugins": []}
    for n in names:
        doc["plugins"].append({
            "name": n,
            "source": {"type": "bundled", "repo": "o/r", "ref": "main",
                       "revision": "git:" + "e" * 40, "subdir": ""},
            "artifact_id": "c" * 64, "version": "1.0.0",
            "targets": ["executor:plugin-developer"]})
    (Path(defaults_dir) / "plugin-registry.json").write_text(
        json.dumps(doc), encoding="utf-8")


def _agent_home(homes, role, enabled):
    p = Path(homes) / role / ".claude"
    p.mkdir(parents=True, exist_ok=True)
    (p / "settings.json").write_text(
        json.dumps({"enabledPlugins": enabled}), encoding="utf-8")


def _run(tmp_path, monkeypatch, d, **kw):
    monkeypatch.setattr(pm, "_config_git_untrack", lambda *a, **k: None)
    return pm.run_migration(
        cc_home=d["cc_home"], config_dir=d["config"], data_dir=d["data"],
        defaults_dir=d["defaults"], agents_dir=d["agents"],
        agent_home_root=d["homes"], store_root=d["store"],
        staging_root=d["staging"],
        registry_path=d["plugins"] / "registry.json",
        sentinel_path=d["plugins"] / ".migration-done",
        report_path=d["data"] / "report.json", **kw)


def test_online_happy_user_plugin(tmp_path, monkeypatch):
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], [])
    (d["config"] / "marketplace" / ".claude-plugin").mkdir(parents=True)
    (d["config"] / "marketplace" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "lesina-invoice",
                                 "source": {"repo": "bonzanni/x", "ref": "master"}}]}))
    (Path(d["agents"]) / "specialists" / "finance").mkdir(parents=True)
    _agent_home(d["homes"], "finance", {"lesina-invoice@casa-plugins": True})
    monkeypatch.setattr(pm, "_installed_state",
                        lambda cc: {"lesina-invoice": {"installPaths": {"/x"},
                                                       "scopes": {"project"},
                                                       "enabled": True}})
    monkeypatch.setattr(pm.plugin_store, "publish",
                        lambda **kw: _pr("lesina-invoice"))
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    reg = json.loads((d["plugins"] / "registry.json").read_text())
    entry = next(e for e in reg["plugins"] if e["name"] == "lesina-invoice")
    assert entry["targets"] == ["specialist:finance"]
    assert entry["source"]["revision"].startswith("git:")
    assert (d["plugins"] / ".migration-done").is_file()
    assert report["migrated"][0]["origin"] == "github"


def test_phantom_ref_is_issue_others_migrate(tmp_path, monkeypatch):
    from plugin_store import RefNotFound
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], [])
    (d["config"] / "marketplace" / ".claude-plugin").mkdir(parents=True)
    (d["config"] / "marketplace" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [
            {"name": "gone", "source": {"repo": "o/gone", "ref": "phantom"}},
            {"name": "ok", "source": {"repo": "o/ok", "ref": "v1"}}]}))
    (Path(d["agents"]) / "specialists" / "finance").mkdir(parents=True)
    _agent_home(d["homes"], "finance",
                {"gone@casa-plugins": True, "ok@casa-plugins": True})
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {
        "gone": {"installPaths": {"/g"}, "scopes": {"project"}, "enabled": True},
        "ok": {"installPaths": {"/o"}, "scopes": {"project"}, "enabled": True}})

    def _publish(*, name, **kw):
        if name == "gone":
            raise RefNotFound("404")
        return _pr(name)
    monkeypatch.setattr(pm.plugin_store, "publish", _publish)
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    assert any(i["reason_code"] == "ref_not_found" and i["name"] == "gone"
               for i in report["issues"])
    reg = json.loads((d["plugins"] / "registry.json").read_text())
    assert [e["name"] for e in reg["plugins"]] == ["ok"]


def test_offline_dirty_adopts_legacy_content(tmp_path, monkeypatch):
    from plugin_store import ResolveUnavailable
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], [])
    (d["config"] / "marketplace" / ".claude-plugin").mkdir(parents=True)
    (d["config"] / "marketplace" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "lesina",
                                 "source": {"repo": "o/l", "ref": "master"}}]}))
    install = tmp_path / "install"
    install.mkdir()
    (Path(d["agents"]) / "specialists" / "finance").mkdir(parents=True)
    _agent_home(d["homes"], "finance", {"lesina@casa-plugins": True})
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {
        "lesina": {"installPaths": {str(install)}, "scopes": {"project"},
                   "enabled": True}})
    monkeypatch.setattr(pm.plugin_store, "publish",
                        lambda **kw: (_ for _ in ()).throw(ResolveUnavailable("net")))
    monkeypatch.setattr(pm, "_offline_revision", lambda p, repo: ("", True))
    monkeypatch.setattr(pm.plugin_store, "publish_legacy_tree",
                        lambda **kw: _pr("lesina",
                                         revision="legacy-content:" + "f" * 64))
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    reg = json.loads((d["plugins"] / "registry.json").read_text())
    entry = next(e for e in reg["plugins"] if e["name"] == "lesina")
    assert entry["source"]["revision"].startswith("legacy-content:")
    assert any(w["reason_code"] == "legacy_provenance" for w in report["warnings"])


def test_disablement_wins_over_default(tmp_path, monkeypatch):
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], ["superpowers"])
    (Path(d["agents"]) / "executors" / "plugin-developer").mkdir(parents=True)
    # A resident explicitly DISABLES superpowers; the default targets executor,
    # but the resident target must never be resurrected.
    _agent_home(d["homes"], "assistant",
                {"superpowers@casa-plugins-defaults": False})
    (Path(d["agents"]) / "assistant").mkdir(parents=True)
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    reg = json.loads((d["plugins"] / "registry.json").read_text())
    entry = next((e for e in reg["plugins"] if e["name"] == "superpowers"), None)
    # superpowers keeps its executor default target but NOT resident:assistant.
    assert entry is not None
    assert "resident:assistant" not in entry["targets"]


def test_executor_plugins_yaml_authoritative(tmp_path, monkeypatch):
    import yaml
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], ["superpowers", "plugin-dev", "context7"])
    ex = Path(d["agents"]) / "executors" / "plugin-developer"
    ex.mkdir(parents=True)
    # Lists only 2 of the 3 defaults → the omitted one is disabled here.
    (ex / "plugins.yaml").write_text(yaml.safe_dump({"plugins": [
        {"name": "superpowers"}, {"name": "context7"}]}))
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    reg = json.loads((d["plugins"] / "registry.json").read_text())
    by = {e["name"]: e for e in reg["plugins"]}
    assert "executor:plugin-developer" in by["superpowers"]["targets"]
    assert "executor:plugin-developer" in by["context7"]["targets"]
    # plugin-dev was omitted → no executor target (default suppressed).
    assert "plugin-dev" not in by or \
        "executor:plugin-developer" not in by["plugin-dev"]["targets"]


def test_seeded_defaults_covers_all(tmp_path, monkeypatch):
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], ["superpowers", "context7"])
    (Path(d["agents"]) / "executors" / "plugin-developer").mkdir(parents=True)
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    reg = json.loads((d["plugins"] / "registry.json").read_text())
    assert set(reg["seeded_defaults"]) == {"superpowers", "context7"}


def test_unreadable_registry_skips_migration(tmp_path, monkeypatch):
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], [])
    corrupt = d["plugins"] / "registry.json"
    corrupt.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    assert corrupt.read_text(encoding="utf-8") == "{broken"     # untouched
    assert not (d["plugins"] / ".migration-done").is_file()     # NO sentinel
    assert any(i["reason_code"] == "registry_invalid_migration_skipped"
               for i in report["issues"])


def test_report_before_sentinel_and_idempotent(tmp_path, monkeypatch):
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], ["superpowers"])
    (Path(d["agents"]) / "executors" / "plugin-developer").mkdir(parents=True)
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    real_touch = pm._atomic_touch
    # First run: sentinel write fails → report exists, sentinel absent, no raise.
    monkeypatch.setattr(pm, "_atomic_touch",
                        lambda p: (_ for _ in ()).throw(OSError("disk full")))
    _run(tmp_path, monkeypatch, d)
    assert (d["data"] / "report.json").is_file()               # report first
    assert not (d["plugins"] / ".migration-done").is_file()    # sentinel absent
    reg1 = json.loads((d["plugins"] / "registry.json").read_text())
    # Re-run (sentinel works) → converges to the SAME registry (idempotent).
    monkeypatch.setattr(pm, "_atomic_touch", real_touch)
    _run(tmp_path, monkeypatch, d)
    reg2 = json.loads((d["plugins"] / "registry.json").read_text())
    assert [e["name"] for e in reg1["plugins"]] == [e["name"] for e in reg2["plugins"]]


def test_wholesale_exception_withholds_sentinel(tmp_path, monkeypatch):
    """Sol #3: a mid-flight _migrate crash leaves `data` UNSAVED, so the sentinel
    must be WITHHELD (not written) or migration is permanently skipped with no
    retry and the registry frozen unmigrated."""
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], ["superpowers"])
    (Path(d["agents"]) / "executors" / "plugin-developer").mkdir(parents=True)
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    monkeypatch.setattr(pm, "_migrate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    assert any(i["reason_code"] == "migration_exception" for i in report["issues"])
    assert not (d["plugins"] / ".migration-done").is_file()      # sentinel withheld
    assert not (d["plugins"] / "registry.json").is_file()        # data not saved


def test_enabled_plugins_list_is_scoped_issue_not_crash(tmp_path, monkeypatch):
    """Sol #3: a non-dict `enabledPlugins` (e.g. a hand-edited list) must become
    a scoped issue, NOT crash the whole migration into a permanent skip."""
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], ["superpowers"])
    (Path(d["agents"]) / "executors" / "plugin-developer").mkdir(parents=True)
    (Path(d["agents"]) / "assistant").mkdir(parents=True)
    home = Path(d["homes"]) / "assistant" / ".claude"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(
        json.dumps({"enabledPlugins": ["superpowers@x"]}), encoding="utf-8")
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    assert (d["plugins"] / ".migration-done").is_file()          # completed → sentinel
    assert any(i["reason_code"] == "enabled_plugins_malformed" for i in report["issues"])
    assert not any(i["reason_code"] == "migration_exception" for i in report["issues"])


def test_divergent_installpaths_refused(tmp_path, monkeypatch):
    """Sol #10: multiple distinct installPaths for one name → refuse to adopt an
    arbitrary (sorted-first) one; record the divergence, leave it unassigned."""
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], [])
    (d["config"] / "marketplace" / ".claude-plugin").mkdir(parents=True)
    (d["config"] / "marketplace" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "dup",
                                 "source": {"repo": "o/d", "ref": "v1"}}]}))
    (Path(d["agents"]) / "specialists" / "finance").mkdir(parents=True)
    _agent_home(d["homes"], "finance", {"dup@casa-plugins": True})
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {
        "dup": {"installPaths": {"/a", "/b"}, "scopes": {"user", "project"},
                "enabled": True}})
    monkeypatch.setattr(pm.plugin_store, "publish",
                        lambda **kw: pytest.fail("must not adopt a divergent plugin"))
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    assert any(i["reason_code"] == "install_path_divergence" and i["name"] == "dup"
               for i in report["issues"])
    reg = json.loads((d["plugins"] / "registry.json").read_text())
    assert not any(e["name"] == "dup" for e in reg["plugins"])   # NOT adopted


def test_append_idempotent_no_duplicate_names(tmp_path, monkeypatch):
    """Sol #1 defense-in-depth: even against a pre-populated registry (the old
    seed-before-migrate order), migration never appends a duplicate name."""
    d = _dirs(tmp_path)
    _default_registry(d["defaults"], ["superpowers"])
    (Path(d["agents"]) / "executors" / "plugin-developer").mkdir(parents=True)
    reg_path = d["plugins"] / "registry.json"
    reg_path.write_text(json.dumps({
        "schema_version": 1, "seeded_defaults": ["superpowers"],
        "plugins": [{"name": "superpowers",
                     "source": {"type": "bundled", "repo": "o/r", "ref": "main",
                                "revision": "git:" + "e" * 40, "subdir": ""},
                     "artifact_id": "c" * 64, "version": "1.0.0",
                     "targets": ["executor:plugin-developer"]}]}), encoding="utf-8")
    monkeypatch.setattr(pm, "_installed_state", lambda cc: {})
    report, issues, warnings = _run(tmp_path, monkeypatch, d)
    reg = json.loads(reg_path.read_text())
    assert [e["name"] for e in reg["plugins"]].count("superpowers") == 1
