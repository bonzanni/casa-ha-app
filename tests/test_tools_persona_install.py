"""Task N1d Step 6 (task-n1d-brief requirement 6): tool-level tests for the
configurator MCP tools persona_install_commit/persona_apply (tools.py),
mirroring tests/test_tools_specialist_install.py's patterns: monkeypatch
network/disk-touching pieces only, never touch the real /data or /config
paths."""
from __future__ import annotations

import json
from pathlib import Path

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


@pytest.mark.asyncio
async def test_persona_install_commit_returns_version_content_conflict_kind(
    monkeypatch, tmp_path,
) -> None:
    """(c) Fix-round-1 CRITICAL regression, tool level: committing the SAME
    persona_id@version a second time with DIFFERENT, freshly-approved
    content must surface through persona_install_commit as ok:False
    kind:"version_content_conflict" — never an unstructured exception, and
    never a silent ok:True carrying the FIRST install's stale bytes."""
    from test_persona_install import _write_persona_repo
    from persona_pack import load_persona_pack
    import persona_install
    from persona_install import PersonaInstallAckStore
    from tools import persona_install_commit

    tmp_acks_path = tmp_path / "acks.json"

    class _TmpAckStore(PersonaInstallAckStore):
        def __init__(self, path=None):  # noqa: ARG002 — tool always calls with no args
            super().__init__(path=tmp_acks_path)

    monkeypatch.setattr(persona_install, "PersonaInstallAckStore", _TmpAckStore)

    # Same seam as the ack-store redirect above: the tool's `commit_persona_
    # install(...)` call never passes personas_root, so it defaults to the
    # real /config/personas — redirect it to tmp_path while still running
    # the REAL commit_persona_install logic (the thing under test), not a
    # stand-in that would hide a regression.
    real_commit = persona_install.commit_persona_install
    personas_root = tmp_path / "personas"

    def _commit_with_tmp_root(*, inspection, acks):
        return real_commit(inspection=inspection, acks=acks, personas_root=personas_root)

    monkeypatch.setattr(persona_install, "commit_persona_install", _commit_with_tmp_root)

    async def _ack_and_commit_via_tool(staged: Path, pack) -> dict:
        acks = _TmpAckStore()
        identity = persona_install.persona_install_consent_identity(
            persona_id=pack.persona_id, version=pack.version, checksum=pack.checksum)
        acks.record(identity=identity, persona_id=pack.persona_id, version=pack.version,
                     checksum=pack.checksum)
        result = await persona_install_commit.handler({
            "persona_id": pack.persona_id, "version": pack.version, "checksum": pack.checksum,
            "staged_dir": str(staged),
        })
        return _payload(result)

    staged1 = tmp_path / "staged1"
    _write_persona_repo(staged1)
    pack1 = load_persona_pack(staged1 / "pack", staged1 / "manifest.json")
    first = await _ack_and_commit_via_tool(staged1, pack1)
    assert first["ok"] is True

    staged2 = tmp_path / "staged2"
    _write_persona_repo(staged2, negative_space="Always double-checks the units.")
    pack2 = load_persona_pack(staged2 / "pack", staged2 / "manifest.json")
    assert pack2.checksum != pack1.checksum  # same persona_id@version, genuinely different content

    second = await _ack_and_commit_via_tool(staged2, pack2)
    assert second["ok"] is False
    assert second["kind"] == "version_content_conflict"

    # dest bytes are still the FIRST, approved install's — never clobbered.
    dest = personas_root / pack1.persona_id / pack1.version
    reloaded = load_persona_pack(dest / "pack", dest / "manifest.json")
    assert reloaded.checksum == pack1.checksum


@pytest.mark.asyncio
async def test_persona_apply_on_a_pending_configuration_specialist_reports_no_active_tuple(
    monkeypatch, tmp_path,
) -> None:
    """Fix-round-1 IMPORTANT regression: installed_component_role_dirs()
    legitimately resolves a pending-configuration specialist (desired-only,
    active=None — a real state per specialist_registry.py's own docstring),
    so persona_apply proceeds into apply_persona_override, whose specialist
    branch raises SpecialistInstallError("no_active_tuple", ...). Before
    this fix, persona_apply only caught ValueError, so that exception
    escaped unstructured instead of the tool's {ok, kind} contract."""
    from test_persona_install import _write_specialist_component
    import persona_pack
    import specialist_registry
    from tools import persona_apply

    slug = "pending-n1d"
    component_root = _write_specialist_component(tmp_path / "component", slug=slug)

    class _FakePack:
        persona_id = "casa/newton"
        version = "0.1.0"

    class _FakeIndex:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def load(self) -> None:
            pass

        def installed_component_role_dirs(self) -> dict:
            # Mirrors the real method's "active-or-desired" fallback for a
            # pending-configuration slug: the role artifact resolves even
            # though no active tuple exists yet at instance_dir_root
            # (Path("/config/specialists")/slug, which this sandbox has no
            # real directory for — so InstanceDir(...).active() naturally
            # returns None, exactly like a genuine pending-configuration
            # specialist whose desired.yaml was staged but never committed).
            return {slug: component_root}

    monkeypatch.setattr(specialist_registry, "InstalledSpecialistIndex", _FakeIndex)
    monkeypatch.setattr(persona_pack, "load_persona_pack", lambda *a, **k: _FakePack())

    result = await persona_apply.handler({
        "target_role_id": f"specialist:{slug}",
        "persona_id": "casa/newton", "persona_version": "0.1.0",
    })

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["kind"] == "no_active_tuple"
