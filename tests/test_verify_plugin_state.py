"""§3.9 tier-aware verify_plugin_state: desired (registry) vs active (running)
agreement. Verification can never report active agreement while a running
consumer executes different code (FR3); dormant targets report configured
readiness; executors also need their plugin MCP namespaces authorized."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin_fixtures import entry, mk_artifact, mk_registry

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point the system-requirements manifest + plugin-env.conf at absent tmp
    files so production /config state can't leak into a test."""
    import system_requirements.manifest as mani
    import plugin_env_conf as pec
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")


def _verify(tmp_path, name="probe", tools_bin=None):
    from tools import _tool_verify_plugin_state
    return _tool_verify_plugin_state(
        plugin_name=name,
        _registry_path=tmp_path / "registry.json",
        _store_root=tmp_path / "store",
        _tools_bin=tools_bin)


class _Agent:
    def __init__(self, binding, resolved=True):
        self.active_plugin_binding = dict(binding)
        self._plugin_resolution = object() if resolved else None


def _runtime(agents=None, executor_registry=None):
    return SimpleNamespace(agents=agents or {},
                           executor_registry=executor_registry)


def test_verify_dormant_ready(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True
    assert r["targets"][0]["state"] == "dormant"
    assert r["targets"][0]["ready"] is True


def test_verify_not_registered(tmp_path):
    mk_registry(tmp_path, [])
    r = _verify(tmp_path)
    assert r["ready"] is False and r["reasons"] == ["not_registered"]


def test_verify_corrupt_artifact(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    root = mk_artifact(store, "probe", e["artifact_id"])
    (root / "tampered.md").write_text("evil", encoding="utf-8")   # post-publish
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert "corrupt_artifact" in r["reasons"]


def test_verify_skill_only_ready(tmp_path):
    """R-1: a plugin with no .mcp.json is still ready (no required env)."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])          # no mcp_servers
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True and r["secrets"] == []


def test_verify_missing_verify_bin(tmp_path, monkeypatch):
    import system_requirements.manifest as mani
    import yaml
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    (tmp_path / "sysreq.yaml").write_text(yaml.safe_dump({"plugins": [
        {"name": "probe", "winning_strategy": "tarball", "verify_bin": "ffmpeg"}]}))
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    r = _verify(tmp_path, tools_bin=tmp_path / "empty-bin")
    assert r["ready"] is False
    assert r["tools"][0]["status"] == "missing"


def test_verify_missing_secret(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {"env": {"K": "${MY_API_KEY}"}}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert r["secrets"][0]["var"] == "MY_API_KEY"
    assert r["secrets"][0]["status"] == "unresolved"


def test_verify_reload_required_is_never_green(tmp_path, monkeypatch):
    """THE incident assertion: a constructed agent still bound to the OLD
    artifact after a registry update must report reload_required — verify can
    never green a stale binding (FR3)."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])           # NEW artifact_id
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    stale = _Agent({"probe": "old" * 21 + "o"})           # bound to OLD id
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents={"finance": stale}), raising=False)
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert r["stale_targets"] == ["specialist:finance"]
    row = r["targets"][0]
    assert row["state"] == "active" and row["reasons"] == ["reload_required"]

    # After reconstruction (binding == desired) → ready.
    fresh = _Agent({"probe": e["artifact_id"]})
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(agents={"finance": fresh}), raising=False)
    assert _verify(tmp_path)["ready"] is True


def test_verify_authorization_missing_vs_authorized(tmp_path, monkeypatch):
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["executor:plugin-developer"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"probe": {}})               # grant mcp__plugin_probe_probe
    mk_registry(tmp_path, [e])

    class _ExecReg:
        def __init__(self, allowed):
            self._allowed = allowed

        def get(self, t):
            return SimpleNamespace(tools_allowed=self._allowed)

    # Missing the namespace → authorization_missing, not ready.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(executor_registry=_ExecReg(["Read"])),
                        raising=False)
    r = _verify(tmp_path)
    assert r["ready"] is False
    row = r["targets"][0]
    assert row["reasons"] == ["authorization_missing"]
    assert row["authorization"]["missing"] == ["mcp__plugin_probe_probe"]

    # Namespace granted → ready.
    monkeypatch.setattr(
        agent_mod, "active_runtime",
        _runtime(executor_registry=_ExecReg(["mcp__plugin_probe_probe"])),
        raising=False)
    assert _verify(tmp_path)["ready"] is True


def test_verify_running_engagement_on_previous_artifact_is_informational(
        tmp_path, monkeypatch):
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("probe", ["executor:plugin-developer"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    rec = SimpleNamespace(id="eng1", plugin_artifacts=[
        {"name": "probe", "artifact_id": "old" * 21 + "o"}])
    fake_reg = SimpleNamespace(active_and_idle=lambda: [rec])
    monkeypatch.setattr(tools_mod, "_engagement_registry", fake_reg)
    r = _verify(tmp_path)
    assert r["ready"] is True                     # informational, not blocking
    assert r["sessions_on_previous_artifact"] == [
        {"engagement_id": "eng1", "artifact_id": "old" * 21 + "o"}]


def test_verify_provenance_warning_on_legacy_content(tmp_path):
    store = tmp_path / "store"
    rev = "legacy-content:" + "c" * 64
    e = entry("probe", ["specialist:finance"], revision=rev)
    mk_artifact(store, "probe", e["artifact_id"], revision=rev)
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is True                     # warning is not a blocker
    assert r["artifact"]["provenance_warning"] is True


def test_verify_plugin_secrets_shim(tmp_path, monkeypatch):
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    orig = tools_mod._tool_verify_plugin_state
    monkeypatch.setattr(
        tools_mod, "_tool_verify_plugin_state",
        lambda *, plugin_name: orig(
            plugin_name=plugin_name, _registry_path=tmp_path / "registry.json",
            _store_root=tmp_path / "store"))
    out = tools_mod._tool_verify_plugin_secrets(plugin_name="probe")
    assert "secrets" in out and isinstance(out["secrets"], list)


def test_bundled_registry_authorized_for_plugin_developer(tmp_path, monkeypatch):
    """Sol R2-5 (fast unit guard, NOT the proof): the checked-in default
    registry × the real plugin-developer definition — grants derived from
    fixture artifacts mirroring each bundled plugin's .mcp.json server keys
    must be fully authorized. (context7 is the only MCP-bearing default.)"""
    import json
    import agent as agent_mod
    import yaml

    root = Path(__file__).resolve().parent.parent / "casa-agent" / "rootfs" / "opt" / "casa"
    default_reg_path = root / "defaults" / "plugin-registry.json"
    if not default_reg_path.is_file():
        pytest.skip("default plugin-registry.json lands in Task 18")
    default_reg = json.loads(default_reg_path.read_text(encoding="utf-8"))
    defn_raw = yaml.safe_load(
        (root / "defaults" / "agents" / "executors" / "plugin-developer"
         / "definition.yaml").read_text(encoding="utf-8"))
    allowed = list((defn_raw.get("tools") or {}).get("allowed") or [])

    store = tmp_path / "store"
    reg_entries = []
    for e in default_reg["plugins"]:
        servers = {"context7": {}} if e["name"] == "context7" else None
        src = e["source"]
        ent = entry(e["name"], e["targets"], revision=src["revision"],
                    subdir=src.get("subdir", ""))
        mk_artifact(store, e["name"], ent["artifact_id"],
                    revision=src["revision"], subdir=src.get("subdir", ""),
                    mcp_servers=servers)
        reg_entries.append(ent)
    mk_registry(tmp_path, reg_entries)

    class _ExecReg:
        def get(self, t):
            return SimpleNamespace(tools_allowed=allowed)

    monkeypatch.setattr(agent_mod, "active_runtime",
                        _runtime(executor_registry=_ExecReg()), raising=False)
    for e in reg_entries:
        r = _verify(tmp_path, name=e["name"])
        row = r["targets"][0]
        assert row["authorization"]["missing"] == [], (
            f"{e['name']}: unauthorized {row['authorization']['missing']}")


# --- Sol review regressions -------------------------------------------------

def _mk_artifact_raw(store, name, artifact_id, *, plugin_json, mcp_text=None,
                     revision="git:" + "a" * 40):
    """Build an artifact writing raw plugin.json / .mcp.json BEFORE the checksum
    (so malformed content is captured as valid-checksum, not corrupt)."""
    import json as _json
    from plugin_store import content_checksum, write_metadata
    root = Path(store) / name / artifact_id
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps(plugin_json), encoding="utf-8")
    if mcp_text is not None:
        (root / ".mcp.json").write_text(mcp_text, encoding="utf-8")
    write_metadata(root, name=name, repo="o/r", ref="v1", revision=revision,
                   subdir="", artifact_id=artifact_id,
                   version=plugin_json.get("version", "1.0.0"),
                   checksum=content_checksum(root))
    return root


def test_verify_duplicate_name_not_green(tmp_path):
    """Sol #8: a duplicate-name entry that resolve_for() drops must not verify
    ready off the raw registry list."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e, dict(e)])           # same name twice
    r = _verify(tmp_path)
    assert r["ready"] is False and r["reasons"] == ["duplicate_name"]


def test_verify_entry_invalid_not_green(tmp_path):
    """Sol #8: a per-entry-invalid registry entry (artifact_id mismatch) that
    the resolver skips must not verify ready."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    e["artifact_id"] = "b" * 64                   # mismatch → entry_invalid
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False and r["reasons"] == ["entry_invalid"]


def test_verify_malformed_mcp_json_not_green(tmp_path):
    """Sol #16: a present-but-malformed .mcp.json (broken MCP server) must not
    verify ready even though grants/secrets silently degrade to []."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    _mk_artifact_raw(store, "probe", e["artifact_id"],
                     plugin_json={"name": "probe", "version": "1.0.0"},
                     mcp_text="{ this is : not valid json")
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False and "mcp_invalid" in r["reasons"]


def test_verify_declared_sysreq_not_installed_not_green(tmp_path):
    """Sol #11: a plugin declaring a systemRequirement with no installed
    manifest row must not verify ready on all([])."""
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    _mk_artifact_raw(store, "probe", e["artifact_id"], plugin_json={
        "name": "probe", "version": "1.0.0",
        "casa": {"systemRequirements": [{"type": "tarball", "verify_bin": "ffmpeg"}]}})
    mk_registry(tmp_path, [e])
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert any(t["status"] == "missing" and t["verify_bin"] == "ffmpeg"
               for t in r["tools"])


def test_postcondition_present_requires_top_ready():
    """Sol #9: zero target rows must not vacuously pass all([]) — the postcondition
    now gates on the top-level readiness (which folds in artifact/tools/secrets
    AND every row)."""
    from tools import _postcondition_holds
    # Zero rows + not ready (e.g. unresolved secret on an unassigned plugin) →
    # must be False (was vacuously True).
    assert _postcondition_holds({"ready": False, "targets": []}, [],
                                expect="present") is False
    # Zero rows but fully configured-ready → True.
    assert _postcondition_holds({"ready": True, "targets": []}, [],
                                expect="present") is True
    # A ready plugin with rows → True.
    v = {"ready": True,
         "targets": [{"target": "specialist:finance", "ready": True}]}
    assert _postcondition_holds(v, ["specialist:finance"],
                                expect="present") is True
    # Top-level not ready even though the listed row is ready → False.
    v2 = {"ready": False,
          "targets": [{"target": "specialist:finance", "ready": True}]}
    assert _postcondition_holds(v2, ["specialist:finance"],
                                expect="present") is False


def test_regenerate_health_preserves_other_plugins_runtime_issue(monkeypatch):
    """Sol #13: a successful mutation of B must not erase A's still-active
    runtime issue (reload_required) from the health report."""
    import tools
    import plugin_health
    import plugin_registry
    captured = {}
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(
                            valid=True, entries=[{"name": "A"}, {"name": "B"}]))

    def _verify_stub(*, plugin_name):
        if plugin_name == "A":
            return {"ready": False, "targets": [
                {"target": "specialist:x", "ready": False,
                 "reasons": ["reload_required"]}]}
        return {"ready": True, "targets": [
            {"target": "executor:y", "ready": True}]}

    monkeypatch.setattr(tools, "_tool_verify_plugin_state", _verify_stub)
    monkeypatch.setattr(plugin_health, "write_report",
                        lambda **k: captured.update(k))
    tools._regenerate_plugin_health([])           # mutating B, no extras
    reasons = [i.reason_code for i in captured["issues"]]
    assert "reload_required" in reasons


def test_verify_discloses_draining_agent_on_previous_artifact(tmp_path,
                                                              monkeypatch):
    """Sol #4: a resident/specialist whose in-flight turn is still draining the
    PREVIOUS artifact after a reload is disclosed (informational) — ready stays
    true (the active binding is the new artifact; the old turn is transient)."""
    import agent as agent_mod
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    runtime = _runtime(agents={})
    runtime.draining = [{"role": "finance", "binding": {"probe": "b" * 64}}]
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    r = _verify(tmp_path)
    assert any(s.get("draining_role") == "finance"
               for s in r["sessions_on_previous_artifact"])
    assert r["ready"] is True


def test_reload_track_and_drop_draining():
    """Sol #4: the draining tracker records a swapped-out agent's binding and
    removes it on close; a binding-less agent is not tracked."""
    from reload import _track_draining, _drop_draining
    runtime = SimpleNamespace()
    agent = SimpleNamespace(active_plugin_binding={"p": "aid1"})
    ent = _track_draining(runtime, "finance", agent)
    assert runtime.draining == [{"role": "finance", "binding": {"p": "aid1"}}]
    _drop_draining(runtime, ent)
    assert runtime.draining == []
    assert _track_draining(
        runtime, "x", SimpleNamespace(active_plugin_binding={})) is None


def test_verify_prefers_valid_entry_over_same_name_invalid(tmp_path):
    """Sol round-3 M-shadow: a valid entry must not be shadowed by an unrelated
    same-name invalid entry — verify serves the resolved (valid) one."""
    store = tmp_path / "store"
    valid = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", valid["artifact_id"])
    invalid = dict(valid)
    invalid["artifact_id"] = "z" * 64            # → entry_invalid (id mismatch)
    mk_registry(tmp_path, [invalid, valid])      # invalid first
    r = _verify(tmp_path)
    assert r["ready"] is True                     # valid entry served, not shadowed


def test_regenerate_health_surfaces_verify_exception(monkeypatch):
    """Sol round-3 H13: a verifier crash on one plugin is surfaced as an issue,
    not silently dropped."""
    import tools
    import plugin_health
    import plugin_registry
    captured = {}
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(
                            valid=True, entries=[{"name": "boom"}]))

    def _verify_boom(*, plugin_name):
        raise RuntimeError("verify crashed")

    monkeypatch.setattr(tools, "_tool_verify_plugin_state", _verify_boom)
    monkeypatch.setattr(plugin_health, "write_report",
                        lambda **k: captured.update(k))
    tools._regenerate_plugin_health([])
    assert any(i.reason_code == "verify_exception" for i in captured["issues"])


def test_verify_secret_configured_but_not_in_effective_env_unresolved(
        tmp_path, monkeypatch):
    """Sol round-4: a secret whose op:// ref failed at boot (config present but
    os.environ unset) must verify UNRESOLVED, not falsely green off config presence."""
    import plugin_env_conf as pec
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"],
                mcp_servers={"s": {"env": {"K": "${MY_API_KEY}"}}})
    mk_registry(tmp_path, [e])
    (tmp_path / "plugin-env.conf").write_text(
        "MY_API_KEY=op://vault/item/field\n", encoding="utf-8")
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")
    monkeypatch.delenv("MY_API_KEY", raising=False)          # not resolved at boot
    r = _verify(tmp_path)
    assert r["ready"] is False
    assert r["secrets"][0]["status"] == "unresolved"

    # Now present in the effective env → resolved.
    monkeypatch.setenv("MY_API_KEY", "sk-real-value")
    r2 = _verify(tmp_path)
    assert r2["secrets"][0]["status"] == "resolved"


def test_regenerate_health_preserves_migration_issues(monkeypatch, tmp_path):
    """Sol round-4 HIGH: a mutation's health rewrite must PRESERVE replayed
    unresolved migration issues (an absent refused plugin is in neither
    resolve_all nor reg.entries)."""
    import json
    import tools
    import plugin_health
    import plugin_registry
    import plugin_boot
    captured = {}
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(
                            valid=True, entries=[], raw={"plugins": []}))
    monkeypatch.setattr(plugin_boot, "MIGRATION_REPORT", tmp_path / "r.json")
    (tmp_path / "r.json").write_text(json.dumps({"issues": [
        {"name": "sp", "reason_code": "install_path_divergence",
         "target": None}]}), encoding="utf-8")
    monkeypatch.setattr(plugin_health, "write_report",
                        lambda **k: captured.update(k))
    tools._regenerate_plugin_health([])
    assert any(i.reason_code == "install_path_divergence"
               for i in captured["issues"])


def test_verify_tolerates_manifest_row_without_winning_strategy(tmp_path, monkeypatch):
    """Sol CI-review: a hand-corrupted sysreq manifest row (name but no
    winning_strategy) must not crash verify (defensive access)."""
    import system_requirements.manifest as mani
    import yaml
    store = tmp_path / "store"
    e = entry("probe", ["specialist:finance"])
    mk_artifact(store, "probe", e["artifact_id"])
    mk_registry(tmp_path, [e])
    (tmp_path / "sysreq.yaml").write_text(yaml.safe_dump({"plugins": [
        {"name": "probe", "verify_bin": "ffmpeg"}]}))   # no winning_strategy
    monkeypatch.setattr(mani, "MANIFEST_PATH", tmp_path / "sysreq.yaml")
    r = _verify(tmp_path, tools_bin=tmp_path / "empty")   # must not raise
    assert r["tools"][0]["requirement"] == "unknown"
