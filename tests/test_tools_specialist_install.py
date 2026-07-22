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

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged),
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

    result = await specialist_install_commit.handler({
        "component_id": component.component_id, "version": component.version,
        "slug": component.slug, "staged_dir": str(staged), "root_digest": root_digest,
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

    result = await specialist_upgrade.handler({
        "slug": component.slug, "component_id": component.component_id,
        "version": component.version, "staged_dir": str(staged),
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

    def _boom(*, slug):
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

    calls: list[dict] = []

    def _fake_uninstall(*, slug):
        calls.append({"slug": slug})

    # The tool's local `from specialist_install import uninstall_specialist`
    # re-reads the module attribute at call time — patch it here.
    monkeypatch.setattr(specialist_install, "uninstall_specialist", _fake_uninstall)

    result = await specialist_uninstall.handler({"slug": "mtg"})

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["slug"] == "mtg"
    assert calls == [{"slug": "mtg"}]
