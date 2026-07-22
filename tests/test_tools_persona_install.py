"""Task N1d Step 6 (task-n1d-brief requirement 6): tool-level tests for the
configurator MCP tools persona_install_commit/persona_apply (tools.py),
mirroring tests/test_tools_specialist_install.py's patterns: monkeypatch
network/disk-touching pieces only, never touch the real /data or /config
paths."""
from __future__ import annotations

import json

import pytest


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_persona_install_commit_rejects_a_changed_checksum(monkeypatch, tmp_path) -> None:
    """(a) staged bytes not matching the caller-supplied args -> ok:False
    checksum_changed, WITHOUT ever calling commit_persona_install."""
    from test_persona_install import _write_persona_repo
    import persona_install
    from tools import persona_install_commit

    staged = tmp_path / "staged"
    _write_persona_repo(staged)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("commit_persona_install must never be called on a checksum mismatch")

    monkeypatch.setattr(persona_install, "commit_persona_install", _must_not_be_called)

    result = await persona_install_commit.handler({
        "persona_id": "casa/newton", "version": "0.1.0",
        "checksum": "sha256:" + "f" * 64,  # deliberately wrong
        "staged_dir": str(staged),
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "checksum_changed"


@pytest.mark.asyncio
async def test_persona_install_commit_rejects_without_a_recorded_consent_ack(monkeypatch, tmp_path) -> None:
    """(b) an unrecorded consent -> ok:False consent_missing, never touching
    the real /data acks store."""
    from test_persona_install import _write_persona_repo
    from persona_pack import load_persona_pack
    import persona_install
    from persona_install import PersonaInstallAckStore
    from tools import persona_install_commit

    staged = tmp_path / "staged"
    _write_persona_repo(staged)
    pack = load_persona_pack(staged / "pack", staged / "manifest.json")

    # The tool constructs its ack store via a bare `PersonaInstallAckStore()`
    # call (production default path /data/persona_install_acks.json) — never
    # write to that real path from a test. The tool's local `from
    # persona_install import ... PersonaInstallAckStore` re-reads the module
    # attribute at call time, so patching it here is sufficient — redirect
    # the no-arg construction to a tmp_path file (mirrors
    # test_tools_specialist_install.py's _TmpAckStore pattern).
    tmp_acks_path = tmp_path / "acks.json"

    class _TmpAckStore(PersonaInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool always calls with no args
            super().__init__(path=tmp_acks_path)

    monkeypatch.setattr(persona_install, "PersonaInstallAckStore", _TmpAckStore)

    result = await persona_install_commit.handler({
        "persona_id": pack.persona_id, "version": pack.version, "checksum": pack.checksum,
        "staged_dir": str(staged),
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "consent_missing"
    assert not tmp_acks_path.exists()


@pytest.mark.asyncio
async def test_persona_install_inspect_surfaces_a_structured_failure(monkeypatch) -> None:
    import persona_install
    from specialist_install import SpecialistInstallError
    from tools import persona_install_inspect

    def _boom(*args, **kwargs):
        raise SpecialistInstallError("fetch_failed", "simulated fetch failure")

    # Mirrors test_specialist_install_inspect_surfaces_a_structured_failure:
    # patch the WHOLE inspect function (not the inner resolve_and_fetch
    # primitive) — inspect_persona_repo's own staging_root.mkdir(...) runs
    # BEFORE resolve_and_fetch and defaults to /config/personas/.staging,
    # which this sandbox can't create (no /config here at all).
    monkeypatch.setattr(persona_install, "inspect_persona_repo", _boom)

    result = await persona_install_inspect.handler({"repo": "owner/repo", "ref": "main"})

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "fetch_failed"


@pytest.mark.asyncio
async def test_persona_apply_on_a_non_installed_specialist_target_reports_not_installed(
    monkeypatch,
) -> None:
    """(c) persona_apply on a non-installed specialist target -> ok:False
    not_installed. The tool loads the persona from the (hard-coded, matching
    every other install tool's production-default style) /config/personas
    tree BEFORE branching on target kind — bypass that with a fake pack
    (persona_pack.load_persona_pack is the module attribute the tool's local
    import re-reads at call time) so the specialist branch's
    InstalledSpecialistIndex (which naturally finds nothing under this
    sandbox's nonexistent /config/specialists) is what actually answers."""
    import persona_pack
    from tools import persona_apply

    class _FakePack:
        persona_id = "casa/newton"
        version = "0.1.0"

    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    result = await persona_apply.handler({
        "target_role_id": "specialist:definitely-not-installed",
        "persona_id": "casa/newton", "persona_version": "0.1.0",
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "not_installed"
    assert payload["slug"] == "definitely-not-installed"


@pytest.mark.asyncio
async def test_persona_apply_invalid_target_kind_reports_invalid_target(monkeypatch) -> None:
    """(d) persona_apply invalid target kind -> invalid_target."""
    import persona_pack
    from tools import persona_apply

    class _FakePack:
        persona_id = "casa/newton"
        version = "0.1.0"

    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    result = await persona_apply.handler({
        "target_role_id": "bogus:foo",
        "persona_id": "casa/newton", "persona_version": "0.1.0",
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "invalid_target"
