"""Task N1b Step 26: tool-level tests for the configurator MCP tools
specialist_install_inspect/specialist_install_commit (tools.py). The brief
provides no tests for these tools directly — designed here per its
instructions: (a) a commit whose recomputed root_digest mismatches the
caller-supplied args must refuse without ever calling
commit_specialist_install; (b) a commit with no recorded consent ack must
refuse with kind "consent_missing", never touching the real /data acks
store; (c) an inspect whose underlying inspect_specialist_repo fails must
surface the same structured kind, never raise."""
import json

import pytest


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _inject_fake_receipt(monkeypatch, *, plugins=(), slug="mtg"):
    """Task 10: the commit/upgrade tools require a loadable receipt (by opaque
    id) before anything else. Inject a fake so tests exercising the LATER gates
    (root_digest, consent) reach them. receipt_digest="" so the consent
    identity matches acks recorded with the default receipt_digest.

    Whole-branch D: the fake carries `slug` (matching the component under test)
    so `_assert_receipt_matches_inspection`'s id/digest/slug binding passes — a
    real SourceReceipt always has a slug."""
    import specialist_receipt
    from types import SimpleNamespace
    fake = SimpleNamespace(receipt_id="a" * 32, receipt_digest="", slug=slug,
                           plugins=tuple(plugins))
    monkeypatch.setattr(specialist_receipt, "load", lambda rid, *a, **k: fake)
    return fake


def _stub_bundle_sequencer(monkeypatch):
    """No-op the bundle sequencer + journal-complete so tool-wiring tests don't
    touch the real plugin snapshot / health / journal files."""
    import tools as tools_mod
    import specialist_bundle_journal

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": True, "reloaded": [], "verify": {},
                "reload_errors": [], "removed_artifact_ids": list(removed_artifact_ids)}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)


@pytest.mark.asyncio
async def test_specialist_install_commit_rejects_a_changed_root_digest(
    monkeypatch, tmp_path,
) -> None:
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    import specialist_install
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")

    # commit_specialist_install is the ONLY function that writes into the
    # CAS/specialists tree (its own docstring) — a checksum mismatch must be
    # rejected BEFORE it is ever called, so nothing is persisted. Spy on the
    # module attribute the tool's local `from specialist_install import
    # commit_specialist_install` re-reads at call time.
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "commit_specialist_install must never be called on a root_digest mismatch")

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _must_not_be_called)
    _inject_fake_receipt(monkeypatch)

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged), "receipt_id": "a" * 32,
        "root_digest": "sha256:" + "f" * 64,  # deliberately wrong — never the real digest
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "checksum_changed"


@pytest.mark.asyncio
async def test_specialist_install_commit_rejects_without_a_recorded_consent_ack(
    monkeypatch, tmp_path,
) -> None:
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install_consent
    from specialist_install_consent import SpecialistInstallAckStore
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    # The tool constructs its ack store via a bare `SpecialistInstallAckStore()`
    # call (production default path /data/specialist_install_acks.json) —
    # never write to that real path from a test. The tool's local
    # `from specialist_install_consent import SpecialistInstallAckStore`
    # re-reads the module attribute at call time, so patching it here is
    # sufficient — redirect the no-arg construction to a tmp_path file.
    tmp_acks_path = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool always calls with no args
            super().__init__(path=tmp_acks_path)

    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    _inject_fake_receipt(monkeypatch)

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "consent_missing"
    # Nothing persisted: is_acked() never writes, and consent_missing raises
    # before commit_specialist_install's first CAS/InstanceDir write.
    assert not tmp_acks_path.exists()


@pytest.mark.asyncio
async def test_specialist_install_inspect_surfaces_a_structured_failure(monkeypatch) -> None:
    import specialist_install
    from specialist_install import SpecialistInstallError
    from tools import specialist_install_inspect

    def _boom(*args, **kwargs):
        raise SpecialistInstallError("fetch_failed", "simulated fetch failure")

    # The tool's local `from specialist_install import inspect_specialist_repo`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "inspect_specialist_repo", _boom)

    result = await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "fetch_failed"


# ---------------------------------------------------------------------------
# specialist_upgrade / specialist_rollback / specialist_uninstall (Task N1c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specialist_install_commit_requires_a_receipt_id(monkeypatch, tmp_path) -> None:
    """Task 10: the commit tool loads the trusted receipt by opaque id ONLY;
    a missing/unloadable id fails closed BEFORE any staged bytes are read."""
    from tools import specialist_install_commit

    result = await specialist_install_commit.handler({
        "component_id": "x/y", "version": "0.1.0", "slug": "mtg",
        "staged_dir": str(tmp_path / "staged"), "root_digest": "sha256:" + "a" * 64,
        # no receipt_id
    })
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "receipt_required"


@pytest.mark.asyncio
async def test_commit_sequencer_failure_compensates_with_new_artifact_ids(
    monkeypatch, tmp_path,
) -> None:
    """Task 10 sequencer-failure compensation: when _bundle_reload_and_verify
    raises, the tool rolls the disk state back and re-runs the sequencer with
    the NEW set's artifact ids as `removed` (un-publishing the runtime state),
    completes the journal, then re-raises."""
    from types import SimpleNamespace
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_bundle_journal
    import tools as tools_mod
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    _inject_fake_receipt(monkeypatch)

    rolled_back = []
    completed = []
    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=("NEWAID",),
        journal_path="/tmp/j.json",
        rollback_disk=lambda: rolled_back.append(True))

    def _fake_commit(*a, **k):
        return SimpleNamespace(slug="mtg", state="active"), txn

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _fake_commit)
    monkeypatch.setattr(specialist_bundle_journal, "complete",
                        lambda p: completed.append(p))

    seq_calls = []

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        seq_calls.append(list(removed_artifact_ids))
        if len(seq_calls) == 1:
            raise RuntimeError("reload blew up")
        return {"reloaded": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)

    with pytest.raises(RuntimeError):
        await specialist_install_commit.handler({
            "component_id": component.component_id, "version": component.version,
            "slug": "mtg", "staged_dir": str(staged), "root_digest": root_digest,
            "receipt_id": "a" * 32,
        })

    assert rolled_back == [True]                 # disk restored
    assert seq_calls == [[], ["NEWAID"]]         # compensating pass un-publishes the NEW set
    assert completed == ["/tmp/j.json"]          # journal completed


@pytest.mark.asyncio
async def test_commit_seq_not_ready_compensates_and_reports_ok_false(
    monkeypatch, tmp_path,
) -> None:
    """Whole-branch B: a sequencer that returns ok:false (a not-ready owned
    binding / failed postcondition — NOT an exception) must compensate (roll the
    disk back, un-publish, complete the journal) and surface ok:false, never
    complete the journal + report success."""
    from types import SimpleNamespace
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_bundle_journal
    import tools as tools_mod
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    _inject_fake_receipt(monkeypatch)

    rolled_back = []
    completed = []
    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=("NEWAID",),
        journal_path="/tmp/j.json",
        rollback_disk=lambda: rolled_back.append(True))
    monkeypatch.setattr(specialist_install, "commit_specialist_install",
                        lambda *a, **k: (SimpleNamespace(slug="mtg", state="active"), txn))
    monkeypatch.setattr(specialist_bundle_journal, "complete",
                        lambda p: completed.append(p))

    seq_calls = []

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        seq_calls.append(list(removed_artifact_ids))
        if len(seq_calls) == 1:
            return {"ok": False, "kind": "postcondition_failed", "reloaded": [],
                    "reload_errors": [], "not_ready": ["mtg.mtg"],
                    "absent_violations": [], "verify": {}}
        return {"ok": True, "reloaded": [], "verify": {}}

    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": "mtg", "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    })
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "postcondition_failed"
    assert rolled_back == [True]                  # disk restored
    assert seq_calls == [[], ["NEWAID"]]          # compensating pass un-publishes NEW set
    assert completed == ["/tmp/j.json"]           # journal completed (not left dangling)


@pytest.mark.asyncio
async def test_bundle_sequencer_uninstall_evicts_and_verifies_absent(monkeypatch) -> None:
    """Whole-branch C: the uninstall sequencer runs the full agents add/EVICT
    sweep (not a single-role reconstruct) and fails the postcondition unless the
    removed specialist's agent + scoped names are absent."""
    from types import SimpleNamespace
    import agent as agent_mod
    import plugin_registry
    import reload as reload_mod
    import tools as tools_mod

    calls = []

    async def _dispatch(scope, *, runtime, role=None):
        calls.append((scope, role))
        return {"status": "ok"}

    monkeypatch.setattr(reload_mod, "dispatch", _dispatch)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "load_registry", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(plugin_registry, "owned_entries_for", lambda slug, reg: [])
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda t: SimpleNamespace(plugins=[]))
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda issues: None)

    async def _notify():
        return None

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **k: None)

    # Agent already evicted -> the full agents sweep is dispatched, ok.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(agents={}, agents_dir=None), raising=False)
    seq = await tools_mod._bundle_reload_and_verify(
        "mtg", removed_artifact_ids=[], targets_removed=["specialist:mtg"])
    assert ("agents", None) in calls          # full add/evict sweep, not ("agent","mtg")
    assert seq["ok"] is True

    # Agent STILL registered -> absent violation -> ok False.
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(agents={"mtg": object()}, agents_dir=None),
                        raising=False)
    seq2 = await tools_mod._bundle_reload_and_verify(
        "mtg", removed_artifact_ids=[], targets_removed=["specialist:mtg"])
    assert seq2["ok"] is False
    assert "agent:mtg" in seq2["absent_violations"]


@pytest.mark.asyncio
async def test_uninstall_tool_maps_bundle_required_to_envelope(monkeypatch) -> None:
    # Whole-branch M: a typed refusal from uninstall_specialist must surface as
    # a structured ok:false, not a raw exception.
    import specialist_install
    from specialist_install import SpecialistInstallError
    from tools import specialist_uninstall

    def _boom(*, slug, **kwargs):
        raise SpecialistInstallError("bundle_required", "owned entries present")

    monkeypatch.setattr(specialist_install, "uninstall_specialist", _boom)
    result = await specialist_uninstall.handler({"slug": "mtg"})
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "bundle_required"


@pytest.mark.asyncio
async def test_upgrade_tool_maps_bad_staged_dir(monkeypatch, tmp_path) -> None:
    # Whole-branch M: a vanished/corrupt staged_dir surfaces as staged_dir_invalid
    # (mirrors specialist_install_commit's guard), never a raw OSError.
    from tools import specialist_upgrade
    _inject_fake_receipt(monkeypatch)
    result = await specialist_upgrade.handler({
        "slug": "mtg", "component_id": "c/x", "version": "1.0.0",
        "root_digest": "sha256:" + "a" * 64,
        "staged_dir": str(tmp_path / "does-not-exist"), "receipt_id": "a" * 32,
    })
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "staged_dir_invalid"


@pytest.mark.asyncio
async def test_specialist_upgrade_rejects_a_changed_root_digest(monkeypatch, tmp_path) -> None:
    """Mirrors test_specialist_install_commit_rejects_a_changed_root_digest —
    the same fresh re-validation gate the brief mandates for the upgrade
    tool: a caller-supplied root_digest that no longer matches the reloaded
    staged bytes must refuse BEFORE upgrade_specialist is ever called."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    import specialist_install
    from tools import specialist_upgrade

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("upgrade_specialist must never be called on a root_digest mismatch")

    # The tool's local `from specialist_install import upgrade_specialist`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "upgrade_specialist", _must_not_be_called)
    _inject_fake_receipt(monkeypatch)

    result = await specialist_upgrade.handler({
        "slug": component.slug, "component_id": component.component_id,
        "version": component.version, "staged_dir": str(staged), "receipt_id": "a" * 32,
        "root_digest": "sha256:" + "f" * 64,  # deliberately wrong
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "checksum_changed"


@pytest.mark.asyncio
async def test_specialist_rollback_tool_passes_through_no_prior_tuple(monkeypatch) -> None:
    import specialist_install
    from specialist_install import SpecialistInstallError
    from tools import specialist_rollback

    def _boom(*, slug, **kwargs):
        raise SpecialistInstallError("no_prior_tuple", f"{slug!r} has no retained prior tuple")

    # The tool's local `from specialist_install import rollback_specialist`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "rollback_specialist", _boom)

    result = await specialist_rollback.handler({"slug": "mtg"})

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "no_prior_tuple"


@pytest.mark.asyncio
async def test_specialist_uninstall_tool_calls_uninstall_specialist_and_reports_ok(monkeypatch) -> None:
    import specialist_install
    from tools import specialist_uninstall

    from types import SimpleNamespace

    calls: list[dict] = []

    def _fake_uninstall(*, slug, **kwargs):
        calls.append({"slug": slug})
        return SimpleNamespace(slug=slug, removed_artifact_ids=(), new_artifact_ids=(),
                               journal_path="/tmp/does-not-matter.json")

    # The tool's local `from specialist_install import uninstall_specialist`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "uninstall_specialist", _fake_uninstall)
    _stub_bundle_sequencer(monkeypatch)

    result = await specialist_uninstall.handler({"slug": "mtg"})

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["slug"] == "mtg"
    assert calls == [{"slug": "mtg"}]


# ---------------------------------------------------------------------------
# Round-5b (Sol P1): specialist_install_inspect must verify the consent
# keyboard can actually post — or skip it entirely when the ack ledger
# already holds this exact install identity — instead of returning ok:true
# into a flow that strands forever at commit's consent_missing.
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace


def _fake_inspection(tmp_path, *, plugin_resolutions=(), receipt_digest=""):
    return SimpleNamespace(
        component_id="casa.spec.mtg", version="1.0.0", slug="mtg",
        component_checksum="sha256:" + "a" * 64,
        root_digest="sha256:" + "b" * 64,
        mission="Answer MTG rules questions.",
        default_persona_ref="mtg-judge@1.0.0",
        default_persona_checksum="sha256:" + "c" * 64,
        required_config_names=(), required_secret_names=(),
        dependencies=(), staged_dir=tmp_path / "staged",
        # Fix round 1 (task-12): a real inspect_specialist_repo call ALWAYS
        # populates these (specialist_install.py InspectionResult) — every
        # fake here mirrors that so the ok_payload's receipt_id/
        # receipt_digest/plugins wiring is exercised the same as production.
        # `receipt_digest` defaults to "" (legacy-shaped fake) but callers
        # exercising the fix-round-1 (task-13) pre-auth identity fix pass a
        # real digest — specialist_receipt.compute_receipt_digest's own
        # docstring: a REAL inspect_specialist_repo call ALWAYS issues a
        # non-empty receipt_digest, plugins or not.
        receipt_id="d" * 32, receipt_digest=receipt_digest,
        plugin_resolutions=plugin_resolutions,
    )


def _wire_inspect(monkeypatch, tmp_path, *, channel=None, plugin_resolutions=(),
                   receipt_digest=""):
    """Patch the network/disk seams: inspect returns a fake staged result,
    the ack store lives under tmp_path (never /data), and _channel_manager
    serves ``channel`` (None = no telegram channel configured)."""
    import specialist_install
    import specialist_install_consent
    from specialist_install_consent import SpecialistInstallAckStore
    import tools as tools_mod

    fake = _fake_inspection(
        tmp_path, plugin_resolutions=plugin_resolutions, receipt_digest=receipt_digest)
    monkeypatch.setattr(
        specialist_install, "inspect_specialist_repo", lambda *a, **k: fake)
    tmp_acks = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool calls with no args
            super().__init__(path=tmp_acks)

    monkeypatch.setattr(
        specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    monkeypatch.setattr(
        tools_mod, "_channel_manager", SimpleNamespace(get=lambda name: channel))
    return fake, _TmpAckStore


class _Handle:
    """Stub ChallengeHandle: refused / settled-post outcome / never-settles."""

    def __init__(self, refused=None, settled="posted", hang=False):
        self.refused = refused
        self._settled = settled
        self._hang = hang

    async def settled_post(self):
        if self._hang:
            await asyncio.Event().wait()  # cancelled by the tool's wait_for bound
        return self._settled


@pytest.mark.asyncio
async def test_inspect_without_channel_returns_consent_channel_unavailable(
    monkeypatch, tmp_path,
) -> None:
    from tools import specialist_install_inspect
    _wire_inspect(monkeypatch, tmp_path, channel=None)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_channel_unavailable"
    assert "no telegram channel" in payload["detail"]


@pytest.mark.asyncio
async def test_inspect_with_preacked_ledger_skips_keyboard(
    monkeypatch, tmp_path,
) -> None:
    """Pre-authorized path: valid ledger consent for this EXACT identity
    (the same install_consent_identity binding commit validates) -> ok:true
    with NO keyboard attempt — works with no Telegram at all."""
    import specialist_install_consent
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    fake, tmp_store_cls = _wire_inspect(monkeypatch, tmp_path, channel=None)
    identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug)
    tmp_store_cls().record(
        identity=identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug)

    def _must_not_post(**kwargs):
        raise AssertionError("keyboard must not be attempted on a pre-acked install")

    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _must_not_post)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "pre_authorized"
    assert payload["root_digest"] == fake.root_digest


@pytest.mark.asyncio
async def test_inspect_preacked_with_receipt_digest_is_pre_authorized(
    monkeypatch, tmp_path,
) -> None:
    """Fix round 1 (task-13 e2e finding): a REAL inspect_specialist_repo call
    ALWAYS issues a non-empty receipt_digest (specialist_receipt.compute_
    receipt_digest — plugins or not), and the consent-record path
    (prompt_specialist_install_consent's _on_commit_sync) and the commit/
    upgrade consent gates (specialist_install.py) all bind receipt_digest
    into the identity. Before this fix, specialist_install_inspect's own
    pre-auth check computed the identity WITHOUT receipt_digest, so an ack
    recorded (the normal way, with the digest) was never recognized here —
    the keyboard re-posted, and in a Telegram-less container this fell all
    the way to consent_channel_unavailable. Record the ack exactly the way
    _on_commit_sync does (WITH receipt_digest) and assert inspect now
    recognizes it as pre-authorized with no keyboard attempt."""
    import specialist_install_consent
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    receipt_digest = "sha256:" + "f" * 64
    fake, tmp_store_cls = _wire_inspect(
        monkeypatch, tmp_path, channel=None, receipt_digest=receipt_digest)
    identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug, receipt_digest=receipt_digest)
    tmp_store_cls().record(
        identity=identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug, receipt_digest=receipt_digest)

    def _must_not_post(**kwargs):
        raise AssertionError("keyboard must not be attempted on a pre-acked install")

    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _must_not_post)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "pre_authorized"
    assert payload["receipt_digest"] == receipt_digest


@pytest.mark.asyncio
async def test_inspect_legacy_ack_without_receipt_digest_fails_closed(
    monkeypatch, tmp_path,
) -> None:
    """Converse of the fix above: an ack recorded the OLD/legacy way — its
    identity computed WITHOUT receipt_digest, e.g. by the pre-fix buggy
    inspect code, or a genuinely pre-Task-7 ack — must NOT satisfy a fresh
    inspection that carries a real (non-empty) receipt_digest. Fail-closed:
    the ledger lookup misses, so the tool falls through past the
    pre-authorized branch to the keyboard-post attempt; with no telegram
    channel wired that surfaces as consent_channel_unavailable rather than
    silently treating a digest-less legacy ack as covering the bundled
    closure."""
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    receipt_digest = "sha256:" + "f" * 64
    fake, tmp_store_cls = _wire_inspect(
        monkeypatch, tmp_path, channel=None, receipt_digest=receipt_digest)
    legacy_identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug)  # no receipt_digest — legacy shape
    tmp_store_cls().record(
        identity=legacy_identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug)  # no receipt_digest recorded either

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_channel_unavailable"
    assert "consent" not in payload


@pytest.mark.asyncio
async def test_preacked_receipt_digest_identity_also_satisfies_real_commit(
    tmp_path,
) -> None:
    """Round-trips the fix: the SAME ack (identity computed WITH
    receipt_digest, exactly as inspect now records/checks it) also satisfies
    the real commit_specialist_install consent gate — proving the
    follow-on commit succeeds once inspect's pre-auth check stops rejecting
    a receipt-digest-bound ack. Uses a hand-built InspectionResult (receipt=
    None keeps commit_specialist_install on its legacy, non-bundle-journal
    path) since only the consent-identity gate is under test here."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import (
        InspectionResult, commit_specialist_install, compute_install_root_digest,
        resolve_dependency_closure,
    )
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    receipt_digest = "sha256:" + "f" * 64

    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission=str(component.role.role["mission"]),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps,
        staged_dir=staged, receipt_digest=receipt_digest,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest)
    acks.record(identity=identity, component_id=inspection.component_id,
                version=inspection.version, component_checksum=inspection.root_digest,
                slug=inspection.slug, receipt_digest=inspection.receipt_digest)

    instance = commit_specialist_install(
        inspection=inspection, receipt=None, config={}, secret_names_provided=frozenset(),
        acks=acks, specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "active"


@pytest.mark.asyncio
async def test_inspect_ok_payload_surfaces_receipt_and_plugins(monkeypatch, tmp_path) -> None:
    """Fix round 1 (task-12): specialist_install_commit/specialist_upgrade
    REQUIRE args["receipt_id"] (their MCP schemas mark it "required") — an
    ok_payload missing it would dead-end every real install/upgrade at
    receipt_required. Also asserts the "plugins" tool-payload mirror of what
    the consent DM already enumerates (render_install_consent_message)."""
    from specialist_receipt import PluginReceiptRow
    from specialist_install_consent import install_consent_identity
    from tools import specialist_install_inspect

    row = PluginReceiptRow(
        identifier="mtg-corpus", scoped_name="mtg__mtg-corpus", manifest_name="mtg-corpus",
        version="1.0.0", source_type="plugin", repo="owner/mtg-corpus", ref="main",
        revision="d" * 40, subdir="", content_digest="sha256:" + "e" * 64,
        staged_path=str(tmp_path / "staged" / ".dep-plugins" / "mtg-corpus"),
        mcp_servers=("corpus: python server.py",), protected_tools=("corpus_search",),
        env_names=("MTG_CORPUS_TOKEN",),
    )
    fake, tmp_store_cls = _wire_inspect(
        monkeypatch, tmp_path, channel=None, plugin_resolutions=(row,))
    identity = install_consent_identity(
        component_id=fake.component_id, version=fake.version,
        root_digest=fake.root_digest, slug=fake.slug)
    tmp_store_cls().record(
        identity=identity, component_id=fake.component_id, version=fake.version,
        component_checksum=fake.root_digest, slug=fake.slug)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))

    assert payload["ok"] is True
    assert payload["receipt_id"] == fake.receipt_id
    assert payload["receipt_id"]  # non-empty
    assert payload["receipt_digest"] == fake.receipt_digest
    assert payload["plugins"] == [{
        "scoped_name": "mtg__mtg-corpus", "manifest_name": "mtg-corpus", "version": "1.0.0",
        "mcp_servers": ["corpus: python server.py"], "protected_tools": ["corpus_search"],
        "env_names": ["MTG_CORPUS_TOKEN"],
    }]


@pytest.mark.asyncio
async def test_inspect_receipt_id_round_trips_into_commit(monkeypatch, tmp_path) -> None:
    """Round trip (fix round 1, task-12): the receipt_id
    specialist_install_inspect's ok_payload carries, fed back verbatim as
    args["receipt_id"] into specialist_install_commit, is the id
    specialist_receipt.load is actually called with — proving the one-flow
    install the configurator recipe drives (inspect -> commit) is wired end
    to end rather than dead-ending at receipt_required."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    import specialist_install_consent
    import specialist_receipt
    import tools as tools_mod
    from specialist_install_consent import (
        SpecialistInstallAckStore, install_consent_identity,
    )
    from tools import specialist_install_commit, specialist_install_inspect

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    real_receipt_id = "9" * 32
    fake_inspection = SimpleNamespace(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission="Answer test questions.", default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps,
        staged_dir=staged, receipt_id=real_receipt_id, receipt_digest="",
        plugin_resolutions=(),
    )
    monkeypatch.setattr(
        specialist_install, "inspect_specialist_repo", lambda *a, **k: fake_inspection)
    tmp_acks = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool calls with no args
            super().__init__(path=tmp_acks)

    monkeypatch.setattr(
        specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    monkeypatch.setattr(
        tools_mod, "_channel_manager", SimpleNamespace(get=lambda name: None))
    identity = install_consent_identity(
        component_id=component.component_id, version=component.version,
        root_digest=root_digest, slug=component.slug)
    _TmpAckStore().record(
        identity=identity, component_id=component.component_id, version=component.version,
        component_checksum=root_digest, slug=component.slug)

    inspect_payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert inspect_payload["ok"] is True
    assert inspect_payload["receipt_id"] == real_receipt_id

    load_calls: list[str] = []

    def _load(rid, *a, **k):
        load_calls.append(rid)
        return SimpleNamespace(receipt_id=rid, receipt_digest="", plugins=())

    monkeypatch.setattr(specialist_receipt, "load", _load)

    instance = SimpleNamespace(slug="mtg", state="active")
    txn = SimpleNamespace(
        slug="mtg", removed_artifact_ids=(), new_artifact_ids=("AID1",),
        journal_path=str(tmp_path / "journal.json"))
    monkeypatch.setattr(
        specialist_install, "commit_specialist_install", lambda *a, **k: (instance, txn))
    _stub_bundle_sequencer(monkeypatch)

    commit_payload = _payload(await specialist_install_commit.handler({
        "component_id": inspect_payload["component_id"], "version": inspect_payload["version"],
        "slug": inspect_payload["slug"], "staged_dir": inspect_payload["staged_dir"],
        "root_digest": inspect_payload["root_digest"],
        "receipt_id": inspect_payload["receipt_id"],
    }))

    # The receipt loaded is the EXACT id the inspect payload carried — the
    # round trip works — and the commit reaches success, never dead-ending
    # at receipt_required.
    assert load_calls == [real_receipt_id]
    assert commit_payload["ok"] is True
    assert commit_payload["slug"] == "mtg"


@pytest.mark.asyncio
async def test_inspect_happy_path_posts_keyboard(monkeypatch, tmp_path) -> None:
    import specialist_install_consent
    from tools import specialist_install_inspect

    calls: list[dict] = []

    def _prompt(**kwargs):
        calls.append(kwargs)
        return _Handle(settled="posted")

    _wire_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _prompt)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is True
    assert payload["consent"] == "keyboard_posted"
    assert len(calls) == 1
    assert calls[0]["chat_id"] == 123 and calls[0]["operator_id"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("handle,expected_kind", [
    (_Handle(refused="args_too_large"), "consent_prompt_refused"),
    (_Handle(settled="delivery_failed"), "consent_delivery_failed"),
    (_Handle(settled="inactive"), "consent_prompt_inactive"),
    (_Handle(hang=True), "consent_post_unsettled"),
])
async def test_inspect_post_failures_are_structured(
    monkeypatch, tmp_path, handle, expected_kind,
) -> None:
    import specialist_install_consent
    import tools as tools_mod
    from tools import specialist_install_inspect

    _wire_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent",
        lambda **kwargs: handle)
    # Bounded: shrink the settle bound instead of waiting 30s (and never
    # patch <module>.asyncio.sleep — memory-cage rule).
    monkeypatch.setattr(tools_mod, "_INSTALL_CONSENT_POST_TIMEOUT_S", 0.05)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == expected_kind


@pytest.mark.asyncio
async def test_inspect_prompt_exception_is_structured(monkeypatch, tmp_path) -> None:
    import specialist_install_consent
    from tools import specialist_install_inspect

    def _boom(**kwargs):
        raise RuntimeError("registration blew up")

    _wire_inspect(monkeypatch, tmp_path, channel=SimpleNamespace(chat_id="123"))
    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _boom)

    payload = _payload(await specialist_install_inspect.handler(
        {"repo": "owner/repo", "ref": "main"}))
    assert payload["ok"] is False
    assert payload["kind"] == "consent_prompt_failed"
    assert "registration blew up" in payload["detail"]


# ---------------------------------------------------------------------------
# v0.102.0 (#217): the inspect tool captures the requesting configurator
# engagement into reconcile_cb; on Approve+ack that callback delivers a
# synthetic RESUME turn through the channel's resume-if-needed delivery seam
# (deliver_system_turn) so the install proceeds without a manual operator
# nudge. reconcile_cb runs from the tap-callback finish hook — it must NEVER
# raise into it. These tests drive reconcile_cb directly (captured off the
# prompt kwargs), so they never depend on Telegram delivery mechanics.
# ---------------------------------------------------------------------------


def _capture_reconcile(monkeypatch):
    """Patch prompt_specialist_install_consent to a posting stub that records
    the reconcile_cb the inspect tool built, and return the capture dict."""
    import specialist_install_consent

    cap: dict = {}

    def _prompt(**kwargs):
        cap["reconcile_cb"] = kwargs["reconcile_cb"]
        return _Handle(settled="posted")

    monkeypatch.setattr(
        specialist_install_consent, "prompt_specialist_install_consent", _prompt)
    return cap


def _resume_channel(*, registry, deliver):
    return SimpleNamespace(
        chat_id="123", _engagement_registry=registry, deliver_system_turn=deliver)


@pytest.mark.asyncio
async def test_reconcile_cb_resumes_the_captured_engagement(monkeypatch, tmp_path) -> None:
    from tools import specialist_install_inspect, engagement_var

    delivered: list = []
    rec = SimpleNamespace(id="eng-abc", driver="in_casa")
    registry = SimpleNamespace(get=lambda eid: rec if eid == "eng-abc" else None)

    async def _deliver(r, text):
        delivered.append((r, text))

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        payload = _payload(await specialist_install_inspect.handler(
            {"repo": "owner/repo", "ref": "main"}))
    finally:
        engagement_var.reset(token)
    assert payload["consent"] == "keyboard_posted"

    # The tap-callback finish hook fires reconcile_cb after Approve+ack.
    await cap["reconcile_cb"]()
    assert len(delivered) == 1
    assert delivered[0][0] is rec  # resolved for the captured engagement id
    assert "specialist_install_commit" in delivered[0][1]
    assert "specialist:mtg" in delivered[0][1]  # _fake_inspection slug


@pytest.mark.asyncio
async def test_reconcile_cb_swallows_a_delivery_failure(monkeypatch, tmp_path) -> None:
    from tools import specialist_install_inspect, engagement_var

    rec = SimpleNamespace(id="eng-abc", driver="in_casa")
    registry = SimpleNamespace(get=lambda eid: rec)

    async def _deliver(r, text):
        raise RuntimeError("delivery blew up")

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})
    finally:
        engagement_var.reset(token)

    # Fail-safe: reconcile_cb never propagates into the tap-callback path.
    await cap["reconcile_cb"]()  # must not raise


@pytest.mark.asyncio
async def test_reconcile_cb_is_a_noop_when_the_engagement_is_gone(monkeypatch, tmp_path) -> None:
    from tools import specialist_install_inspect, engagement_var

    delivered: list = []
    registry = SimpleNamespace(get=lambda eid: None)  # engagement TTL-expired / gone

    async def _deliver(r, text):
        delivered.append((r, text))

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    token = engagement_var.set(SimpleNamespace(id="eng-abc"))
    try:
        await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})
    finally:
        engagement_var.reset(token)

    await cap["reconcile_cb"]()
    assert delivered == []


@pytest.mark.asyncio
async def test_reconcile_cb_is_a_noop_when_no_engagement_was_captured(
    monkeypatch, tmp_path,
) -> None:
    from tools import specialist_install_inspect

    delivered: list = []
    rec = SimpleNamespace(id="eng-abc", driver="in_casa")
    registry = SimpleNamespace(get=lambda eid: rec)

    async def _deliver(r, text):
        delivered.append((r, text))

    _wire_inspect(monkeypatch, tmp_path,
                  channel=_resume_channel(registry=registry, deliver=_deliver))
    cap = _capture_reconcile(monkeypatch)

    # engagement_var is left at its default (None) — no configurator context.
    await specialist_install_inspect.handler({"repo": "owner/repo", "ref": "main"})
    await cap["reconcile_cb"]()
    assert delivered == []


@pytest.mark.asyncio
async def test_second_commit_on_an_active_slug_is_a_clean_typed_error(
    monkeypatch, tmp_path,
) -> None:
    """#5 idempotency: a resume turn PLUS a stray manual nudge could each fire
    specialist_install_commit. The second commit, on the now-active slug, must
    fail closed with a clean typed kind the LLM handles — never corrupt state
    or raise unstructured. commit_specialist_install's `_refuse_if_active_present`
    raises SpecialistInstallError("concurrent_mutation"); the tool maps it to
    ok:false/kind."""
    from test_specialist_install import _write_component
    from specialist_component import load_specialist_component
    from specialist_install import compute_install_root_digest, resolve_dependency_closure
    import specialist_install
    from tools import specialist_install_commit

    staged = _write_component(tmp_path / "staged", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())

    # Stand in for commit_specialist_install raising the already-active guard —
    # the SAME SpecialistInstallError("concurrent_mutation") _refuse_if_active_
    # present raises on a second commit of a live slug. Verifies the tool's
    # typed-error mapping, not the CAS machinery (covered in test_specialist_install).
    from specialist_install import SpecialistInstallError

    def _already_active(*args, **kwargs):
        raise SpecialistInstallError(
            "concurrent_mutation", "'mtg': an active install appeared under a "
            "concurrent install while acquiring the lock")

    monkeypatch.setattr(specialist_install, "commit_specialist_install", _already_active)
    _inject_fake_receipt(monkeypatch)

    # Consent must be present so the flow reaches commit_specialist_install.
    import specialist_install_consent
    from specialist_install_consent import (
        SpecialistInstallAckStore, install_consent_identity,
    )
    tmp_acks = tmp_path / "acks.json"

    class _TmpAckStore(SpecialistInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002
            super().__init__(path=tmp_acks)

    monkeypatch.setattr(
        specialist_install_consent, "SpecialistInstallAckStore", _TmpAckStore)
    identity = install_consent_identity(
        component_id=component.component_id, version=component.version,
        root_digest=root_digest, slug=component.slug)
    _TmpAckStore().record(
        identity=identity, component_id=component.component_id, version=component.version,
        component_checksum=root_digest, slug=component.slug)

    payload = _payload(await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged), "root_digest": root_digest,
        "receipt_id": "a" * 32,
    }))
    assert payload["ok"] is False
    assert payload["kind"] == "concurrent_mutation"
