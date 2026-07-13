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
