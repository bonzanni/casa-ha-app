"""§3.9/§3.13 plugin_add + plugin_update: the mutation-sequencing contract
(publish → sysreqs → activate → snapshot-reload → reconstruct → verify →
health) that structurally kills the stale-version incident."""
from __future__ import annotations

import copy
import json

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _State:
    def __init__(self):
        self.log: list[str] = []
        self.raw = {"schema_version": 1, "seeded_defaults": [], "plugins": []}


def _pr(name="probe", version="1.2.0", sysreqs=None):
    from plugin_store import PublishResult
    manifest = {"name": name, "version": version}
    if sysreqs is not None:
        manifest["casa"] = {"systemRequirements": sysreqs}
    return PublishResult(name=name, artifact_id="a" * 64,
                         revision="git:" + "b" * 40, version=version,
                         path=f"/store/{name}/" + "a" * 64, manifest=manifest)


def _entry(name="probe", version="1.0.0"):
    """A valid registered entry (its stored revision predates _pr()'s)."""
    return {"name": name,
            "source": {"type": "github", "repo": "o/r", "ref": "v1",
                       "revision": "git:" + "a" * 40, "subdir": ""},
            "artifact_id": "e" * 64, "version": version,
            "targets": ["resident:assistant"]}


def _wire(monkeypatch, tmp_path, st, *, publish=None, publish_exc=None,
          sysreq_exc=None, dispatch_status="ok", with_runtime=True,
          resolved_sha="b" * 40, resolve_exc=None):
    import tools as tools_mod
    import agent as agent_mod
    import plugin_registry as preg
    import reload as reload_mod
    from plugin_registry import RegistryData, ResolutionResult

    def fake_load(path=None):
        return RegistryData(raw=copy.deepcopy(st.raw), entries=[],
                            entry_issues=[], valid=True)

    def fake_save(data, path=None):
        st.raw = copy.deepcopy(data.raw)
        st.log.append("save")

    def fake_resolve(repo, ref, **k):
        st.log.append("resolve")
        if resolve_exc is not None:
            raise resolve_exc
        return resolved_sha

    def fake_publish(*, name, repo, ref, subdir="", commit=None):
        st.log.append("publish")
        if publish_exc is not None:
            raise publish_exc
        return publish

    def fake_install_req(*, plugin_name, requirements, tools_root):
        st.log.append("install_requirements")
        if sysreq_exc is not None:
            raise sysreq_exc
        return []

    async def fake_dispatch(scope, *, runtime, role=None):
        st.log.append(f"dispatch:{role}")
        return {"status": dispatch_status}

    import system_requirements.manifest as _mani
    monkeypatch.setattr(_mani, "MANIFEST_PATH", tmp_path / "sysreq-manifest.yaml")
    monkeypatch.setattr(preg, "load_registry", fake_load)
    monkeypatch.setattr(preg, "save_registry", fake_save)
    monkeypatch.setattr(preg, "reload_snapshot",
                        lambda: st.log.append("reload_snapshot"))
    monkeypatch.setattr(preg, "resolve_all",
                        lambda: ResolutionResult(registry_valid=True))
    monkeypatch.setattr(tools_mod.plugin_store, "resolve_ref", fake_resolve)
    monkeypatch.setattr(tools_mod.plugin_store, "publish", fake_publish)
    monkeypatch.setattr(tools_mod, "install_requirements", fake_install_req)
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda *, plugin_name: {"ready": True})
    monkeypatch.setattr(reload_mod, "dispatch", fake_dispatch)
    monkeypatch.setattr(agent_mod, "active_runtime",
                        object() if with_runtime else None, raising=False)
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH",
                        str(tmp_path / "plugin-health.json"))
    return tools_mod


async def test_plugin_add_happy_activates_and_sequences(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(sysreqs=[{"type": "tarball", "url": "x"}]))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["artifact_id"] == "a" * 64
    assert payload["version"] == "1.2.0"
    # Registry gained the entry.
    assert [e["name"] for e in st.raw["plugins"]] == ["probe"]
    # §3.9/C.2 ORDER is load-bearing:
    # resolve → publish → sysreqs → save → snapshot → reload.
    assert st.log == ["resolve", "publish", "install_requirements", "save",
                      "reload_snapshot", "dispatch:assistant"]


async def test_plugin_add_ref_not_found_pre_mutation(monkeypatch, tmp_path):
    from plugin_store import RefNotFound
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish_exc=RefNotFound("404"))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "phantom",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "ref_not_found"
    assert st.raw["plugins"] == []            # registry byte-identical
    assert "save" not in st.log and "dispatch:assistant" not in st.log


async def test_plugin_add_resolve_unavailable_distinct(monkeypatch, tmp_path):
    from plugin_store import ResolveUnavailable
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish_exc=ResolveUnavailable("net"))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    assert json.loads(r["content"][0]["text"])["kind"] == "resolve_unavailable"


async def test_plugin_add_sysreq_failure_leaves_registry_unchanged(
        monkeypatch, tmp_path):
    from system_requirements.orchestrator import OrchestrationError
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(sysreqs=[{"type": "tarball", "url": "x"}]),
                      sysreq_exc=OrchestrationError("boom"))
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "system_requirements_failed"
    assert st.raw["plugins"] == []            # activation never happened
    assert "save" not in st.log


async def test_plugin_add_duplicate_name_refused(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append({"name": "probe"})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    assert json.loads(r["content"][0]["text"])["kind"] == "plugin_exists"
    assert "publish" not in st.log            # refused pre-publish


async def test_plugin_add_bad_target_grammar_refused(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1", "targets": ["butler"]})
    assert json.loads(r["content"][0]["text"])["kind"] == "invalid_target"
    assert st.log == []


async def test_plugin_update_derives_version_from_manifest(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append({
        "name": "probe",
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.1.0",
        "targets": ["specialist:finance"]})
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="2.0.0"))
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["version"] == "2.0.0"      # FR5: derived, not supplied
    entry = st.raw["plugins"][0]
    assert entry["version"] == "2.0.0"
    assert entry["artifact_id"] == "a" * 64
    assert entry["source"]["ref"] == "v2"


async def test_plugin_update_installs_new_requirements_before_activation(
        monkeypatch, tmp_path):
    from system_requirements.orchestrator import OrchestrationError
    st = _State()
    st.raw["plugins"].append({
        "name": "probe",
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.1.0", "targets": []})
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(version="2.0.0",
                                  sysreqs=[{"type": "npm", "package": "x"}]),
                      sysreq_exc=OrchestrationError("boom"))
    r = await tools_mod.plugin_update.handler({"name": "probe", "new_ref": "v2"})
    assert json.loads(r["content"][0]["text"])["kind"] == \
        "system_requirements_failed"
    assert st.raw["plugins"][0]["version"] == "1.1.0"   # pointer NOT moved
    assert st.log.index("install_requirements") < len(st.log)
    assert "save" not in st.log


async def test_plugin_update_unknown_name_refused(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_update.handler({"name": "ghost", "new_ref": "v2"})
    assert json.loads(r["content"][0]["text"])["kind"] == "not_registered"
    assert "publish" not in st.log


async def test_reload_dispatch_error_makes_mutation_not_ok(monkeypatch, tmp_path):
    """Sol F7: real dispatch envelope is {'status': 'ok'} — an error status
    counts as a reload failure and the mutation reports ok:false."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert payload["reload_errors"]           # carries the failed target


async def test_failed_mutation_leaves_blocking_health_issue(monkeypatch, tmp_path):
    """R2-4: a failed mutation must persist a blocking health issue, never a
    green report."""
    import plugin_health
    st = _State()
    hp = tmp_path / "plugin-health.json"
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    report = plugin_health.load_report(hp)
    assert any(i["reason_code"] == "reload_failed"
               for i in report["issues"])


async def test_mutation_regenerates_health_report(monkeypatch, tmp_path):
    import plugin_health
    st = _State()
    hp = tmp_path / "plugin-health.json"
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    assert plugin_health.load_report(hp) is not None       # rewritten


async def test_error_core_short_circuits_wrapper(monkeypatch, tmp_path):
    """R2-3: an error sync-core never reaches the reload tail."""
    import tools as tools_mod
    called = {"seq": 0}

    async def spy_seq(*a, **kw):
        called["seq"] += 1
        return {"ok": True}

    monkeypatch.setattr(tools_mod, "_plugin_add_sync",
                        lambda **kw: {"ok": False, "kind": "x"})
    monkeypatch.setattr(tools_mod, "_reload_and_verify_targets", spy_seq)
    r = await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1", "targets": []})
    assert json.loads(r["content"][0]["text"])["kind"] == "x"
    assert called["seq"] == 0                 # reload tail NOT reached


async def test_mutating_tools_do_not_stall_event_loop(monkeypatch, tmp_path):
    import asyncio
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    t = asyncio.create_task(tick())
    await asyncio.sleep(0)
    await tools_mod.plugin_add.handler({
        "name": "probe", "repo": "o/r", "ref": "v1",
        "targets": ["resident:assistant"]})
    t.cancel()
    assert ticks >= 1


# --- Task 12: assign / unassign / remove / list -----------------------------

class _FakeAgent:
    def __init__(self, binding):
        self.active_plugin_binding = dict(binding)
        # A real Agent always exposes these; the mutation's post-reconstruct
        # force-resolve (Sol round-4) calls _get_plugin_resolution when the
        # binding hasn't been captured yet.
        self._plugin_resolution = object()

    async def _get_plugin_resolution(self):
        self._plugin_resolution = object()
        return self._plugin_resolution


class _FakeRuntime:
    def __init__(self, agents):
        self.agents = agents


def _registered(st, name="probe", targets=None):
    st.raw["plugins"].append({
        "name": name,
        "source": {"type": "github", "repo": "o/r", "ref": "v1",
                   "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.0.0",
        "targets": list(targets or [])})


async def test_plugin_assign_roundtrip(monkeypatch, tmp_path):
    st = _State()
    _registered(st, targets=[])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_assign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["was_assigned"] is False
    assert st.raw["plugins"][0]["targets"] == ["specialist:finance"]
    assert "reload_snapshot" in st.log and "dispatch:finance" in st.log


async def test_plugin_assign_idempotent(monkeypatch, tmp_path):
    st = _State()
    _registered(st, targets=["specialist:finance"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_assign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["was_assigned"] is True
    assert "save" not in st.log            # no-op: not re-saved


async def test_plugin_unassign_removes_target(monkeypatch, tmp_path):
    st = _State()
    _registered(st, targets=["specialist:finance", "resident:assistant"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    monkeypatch.setattr(__import__("agent"), "active_runtime",
                        _FakeRuntime({"finance": _FakeAgent({})}), raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["was_assigned"] is True
    assert st.raw["plugins"][0]["targets"] == ["resident:assistant"]


async def test_unassign_postcondition_is_absence(monkeypatch, tmp_path):
    """Sol F7: a reconstructed agent that STILL binds the plugin flips the tool
    to postcondition_failed; a clean one returns ok."""
    st = _State()
    _registered(st, targets=["specialist:finance"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    import agent as agent_mod
    # Stub agent that WRONGLY keeps the binding → postcondition_failed.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _FakeRuntime({"finance": _FakeAgent({"probe": "x"})}),
                        raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    assert json.loads(r["content"][0]["text"])["ok"] is False

    # Reconstructed cleanly (binding gone) → ok.
    st2 = _State(); _registered(st2, targets=["specialist:finance"])
    tools_mod = _wire(monkeypatch, tmp_path, st2, publish=_pr())
    monkeypatch.setattr(agent_mod, "active_runtime",
                        _FakeRuntime({"finance": _FakeAgent({})}), raising=False)
    r = await tools_mod.plugin_unassign.handler({
        "name": "probe", "target": "specialist:finance"})
    assert json.loads(r["content"][0]["text"])["ok"] is True


async def test_plugin_remove_keeps_seeded_defaults(monkeypatch, tmp_path):
    """§3.1 no-resurrection: removing a seeded default keeps its name in
    seeded_defaults so a later seed_defaults does NOT re-add it."""
    import plugin_registry
    st = _State()
    st.raw["seeded_defaults"] = ["probe"]
    _registered(st, name="probe", targets=["executor:plugin-developer"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_remove.handler({"name": "probe"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True and payload["artifact_retained"] is True
    assert st.raw["plugins"] == []
    assert st.raw["seeded_defaults"] == ["probe"]     # untouched


async def test_plugin_remove_unknown_refused(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_remove.handler({"name": "ghost"})
    assert json.loads(r["content"][0]["text"])["kind"] == "not_registered"


async def test_plugin_list_reports_presence_and_seeded(monkeypatch, tmp_path):
    st = _State()
    st.raw["seeded_defaults"] = ["probe"]
    _registered(st, name="probe", targets=["executor:plugin-developer"])
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    r = await tools_mod.plugin_list.handler({})
    payload = json.loads(r["content"][0]["text"])
    assert payload["registry_valid"] is True
    row = payload["plugins"][0]
    assert row["name"] == "probe"
    assert row["seeded_default"] is True
    assert row["artifact_present"] is False           # store dir absent in test
    assert row["targets"] == ["executor:plugin-developer"]


def test_plugin_add_schema_subdir_optional():
    """Sol #15: the plugin_add schema must NOT mark subdir required — the
    shorthand {key: type} form marked every key required, so a root-plugin call
    omitting subdir was rejected by the MCP input validator before the handler
    (which defaults it) ever ran."""
    import tools
    schema = tools.plugin_add.input_schema
    assert schema.get("type") == "object"
    assert "subdir" not in schema["required"]
    assert set(schema["required"]) == {"name", "repo", "ref", "targets"}


def test_plugin_add_sync_rejects_bad_subdir_and_nonstring_target():
    """Sol round-3 M: a bad subdir / non-string target returns an envelope, not
    an uncaught crash outside it."""
    from tools import _plugin_add_sync
    r = _plugin_add_sync(name="p", repo="o/r", ref="v1", subdir="../x",
                         targets=["specialist:finance"])
    assert r == {"ok": False, "kind": "invalid_subdir", "subdir": "../x"}
    r2 = _plugin_add_sync(name="p", repo="o/r", ref="v1", targets=[1])
    assert r2["kind"] == "invalid_target"


def test_install_sysreqs_no_reqs_clears_stale_row(monkeypatch):
    """Sol round-3 M: an update to a manifest with NO requirements clears a stale
    manifest row (add_plugin_entry replaces by name on the has-reqs path)."""
    import tools as tools_mod
    removed = []
    monkeypatch.setattr(tools_mod, "remove_manifest", lambda n: removed.append(n))
    r = tools_mod._install_plugin_sysreqs("p", {"name": "p", "version": "2"})
    assert r is None
    assert removed == ["p"]


# --- C.2 identity guards (v0.74.0) ------------------------------------------


async def test_update_revision_mismatch_aborts_before_everything(
        monkeypatch, tmp_path):
    """C.2 step 2: expected_revision mismatch is a hard abort BEFORE
    publish/sysreqs/registry mutation."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      resolved_sha="c" * 40)
    core = tools_mod._plugin_update_sync(
        name="probe", new_ref="v1.2.0", expected_revision="git:" + "b" * 40)
    assert core == {"ok": False, "kind": "revision_mismatch",
                    "expected_revision": "b" * 40,
                    "resolved_revision": "c" * 40}
    for step in ("publish", "install_requirements", "save"):
        assert step not in st.log, step


async def test_update_tag_version_mismatch_aborts_before_sysreqs(
        monkeypatch, tmp_path):
    """C.2 step 4: a vX.Y.Z ref must equal 'v'+manifest.version — abort
    BEFORE sysreq install and registry mutation."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="1.2.0"))
    core = tools_mod._plugin_update_sync(name="probe", new_ref="v9.9.9")
    assert core["ok"] is False and core["kind"] == "tag_version_mismatch"
    assert "install_requirements" not in st.log and "save" not in st.log


async def test_update_non_tag_ref_skips_tag_version_guard(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(version="1.2.0"))
    core = tools_mod._plugin_update_sync(name="probe", new_ref="master")
    assert core["ok"] is True


async def test_update_matching_tag_and_revision_proceeds_in_order(
        monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(version="1.2.0",
                                  sysreqs=[{"type": "tarball", "url": "x"}]))
    core = tools_mod._plugin_update_sync(
        name="probe", new_ref="v1.2.0", expected_revision="b" * 40)
    assert core["ok"] is True
    assert st.log.index("resolve") < st.log.index("publish") \
        < st.log.index("install_requirements") < st.log.index("save")


async def test_add_revision_mismatch_aborts(monkeypatch, tmp_path):
    """C.2 applies to plugin_add too — abort BEFORE publish, sysreq install,
    and registry mutation (r2-B4: ordering asserted, not just no-save)."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(sysreqs=[{"type": "tarball", "url": "x"}]),
                      resolved_sha="c" * 40)
    core = tools_mod._plugin_add_sync(
        name="probe", repo="o/r", ref="v1.2.0",
        targets=["resident:assistant"], expected_revision="b" * 40)
    assert core["kind"] == "revision_mismatch"
    assert st.log == ["resolve"]              # NOTHING after the guard ran
    for step in ("publish", "install_requirements", "save"):
        assert step not in st.log, step
    assert st.raw["plugins"] == []            # registry byte-identical


async def test_add_tag_version_mismatch_aborts_before_sysreqs_and_save(
        monkeypatch, tmp_path):
    """r2-B7: the add-side tag guard, with the same pre-sysreq/pre-save abort."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr(version="1.2.0",
                                  sysreqs=[{"type": "tarball", "url": "x"}]))
    core = tools_mod._plugin_add_sync(
        name="probe", repo="o/r", ref="v9.9.9",
        targets=["resident:assistant"])
    assert core["ok"] is False and core["kind"] == "tag_version_mismatch"
    assert "install_requirements" not in st.log and "save" not in st.log
    assert st.raw["plugins"] == []            # registry byte-identical


async def test_add_invalid_expected_revision_rejected(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    core = tools_mod._plugin_add_sync(
        name="probe", repo="o/r", ref="v1.2.0",
        targets=["resident:assistant"], expected_revision="not-a-sha")
    assert core == {"ok": False, "kind": "invalid_expected_revision",
                    "expected_revision": "not-a-sha"}


async def test_resolver_taxonomy_maps_to_envelope_kinds(monkeypatch, tmp_path):
    import plugin_store
    st = _State()
    st.raw["plugins"].append(_entry())
    cases = [
        (plugin_store.RefNotFound("x"), {"ok": False, "kind": "ref_not_found"}),
        (plugin_store.ResolveAuthFailed("x"),
         {"ok": False, "kind": "resolve_auth_failed"}),
        (plugin_store.SourceEmpty("x"), {"ok": False, "kind": "source_empty"}),
        (plugin_store.ResolveUnavailable("x", retry_after_s=42.0),
         {"ok": False, "kind": "resolve_unavailable", "retry_after_s": 42.0}),
        (plugin_store.ResolveUnavailable("x"),
         {"ok": False, "kind": "resolve_unavailable"}),
    ]
    for exc, expected in cases:
        tools_mod = _wire(monkeypatch, tmp_path, st, resolve_exc=exc)
        core = tools_mod._plugin_update_sync(name="probe", new_ref="v1.2.0")
        assert core == expected, expected["kind"]


# --- §E pinned mutation envelope (v0.74.0) -----------------------------------


async def test_envelope_pre_activation_failure_is_pinned_shape(
        monkeypatch, tmp_path):
    """Guard failure: pin never moved; kind/verify still present (spec §E)."""
    import plugin_store
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      resolve_exc=plugin_store.RefNotFound("x"))
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v9.9.9"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "ref_not_found"
    assert payload["activation_committed"] is False
    assert payload["runtime_ready"] is False
    assert payload["verify"] == {}
    assert res.get("is_error") is True          # outer MCP flag …
    assert "is_error" not in payload            # … never a payload field


async def test_envelope_committed_but_not_ready(monkeypatch, tmp_path):
    """activation_committed:true + runtime_ready:false = 'pin moved, runtime
    not caught up' — callers retry the RELOAD, never the activation."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v1.2.0"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is False
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert "verify" in payload
    assert res.get("is_error") is True


async def test_envelope_fully_ok_has_kind_none(monkeypatch, tmp_path):
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v1.2.0"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["kind"] is None              # pinned shape: present, None
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is True
    assert payload["verify"] == {"ready": True}
    assert res.get("is_error") is not True


async def test_add_envelope_pre_activation_failure_is_pinned_shape(
        monkeypatch, tmp_path):
    """r2-B6: §E names plugin_add too — same pinned shape on its
    pre-activation failure path."""
    import plugin_store
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      resolve_exc=plugin_store.RefNotFound("x"))
    res = await tools_mod.plugin_add.handler(
        {"name": "probe", "repo": "o/r", "ref": "v9.9.9",
         "targets": ["resident:assistant"]})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["kind"] == "ref_not_found"
    assert payload["activation_committed"] is False
    assert payload["runtime_ready"] is False
    assert payload["verify"] == {}
    assert res.get("is_error") is True
    assert "is_error" not in payload


async def test_add_envelope_fully_ok_has_kind_none(monkeypatch, tmp_path):
    """r2-B6: add-side success carries the full pinned payload."""
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    res = await tools_mod.plugin_add.handler(
        {"name": "probe", "repo": "o/r", "ref": "v1.2.0",
         "targets": ["resident:assistant"]})
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["kind"] is None
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is True
    assert payload["verify"] == {"ready": True}
    assert res.get("is_error") is not True


async def test_add_envelope_committed_but_not_ready(monkeypatch, tmp_path):
    st = _State()
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr(),
                      dispatch_status="error")
    res = await tools_mod.plugin_add.handler(
        {"name": "probe", "repo": "o/r", "ref": "v1.2.0",
         "targets": ["resident:assistant"]})
    payload = json.loads(res["content"][0]["text"])
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is False
    assert payload["ok"] is False
    assert payload["kind"] == "reload_failed"
    assert res.get("is_error") is True


def test_plugin_remove_clears_manifest_row(monkeypatch, tmp_path):
    """Sol round-3 M: removing a plugin drops its system-requirement manifest row."""
    import tools as tools_mod
    st = _State()
    st.raw["plugins"].append({
        "name": "gone", "source": {"type": "github", "repo": "o/r", "ref": "v1",
        "revision": "git:" + "a" * 40, "subdir": ""}, "artifact_id": "c" * 64,
        "version": "1.0.0", "targets": ["specialist:finance"]})
    _wire(monkeypatch, tmp_path, st)
    removed = []
    monkeypatch.setattr(tools_mod, "remove_manifest", lambda n: removed.append(n))
    r = tools_mod._plugin_remove_sync(name="gone")
    assert r["ok"] is True and removed == ["gone"]


async def test_mutation_generation_race_retries_reload_then_fails_explicit(
        monkeypatch, tmp_path):
    """D2: a reloaded target whose snapshot generation disagrees with the
    post-reload snapshot triggers ONE re-dispatch retry (a real
    re-resolution), then explicit snapshot_raced — never graded stale."""
    st = _State()
    st.raw["plugins"].append(_entry())
    tools_mod = _wire(monkeypatch, tmp_path, st, publish=_pr())
    import plugin_registry as preg
    import agent as agent_mod
    from types import SimpleNamespace

    class _StaleSnapAgent:
        # generation pinned at 1; snapshot_generation() below returns 99 —
        # permanently mismatched, so both attempts fail.
        plugin_binding_snapshot = SimpleNamespace(binding={}, generation=1)

    runtime = SimpleNamespace(agents={"assistant": _StaleSnapAgent()})
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    monkeypatch.setattr(preg, "snapshot_generation", lambda: 99)
    res = await tools_mod.plugin_update.handler(
        {"name": "probe", "new_ref": "v1.2.0"})
    payload = json.loads(res["content"][0]["text"])
    assert payload["activation_committed"] is True
    assert payload["runtime_ready"] is False
    assert payload["ok"] is False
    assert payload["kind"] == "snapshot_raced"
    # ONE retry: the agent reload was dispatched twice for the target.
    assert st.log.count("dispatch:assistant") == 2
