from pathlib import Path

import pytest

from specialist_install import DependencyResolution, InspectionResult
from specialist_install_consent import (
    SpecialistInstallAckStore,
    install_consent_identity,
    render_install_consent_message,
)


def _inspection(**overrides) -> InspectionResult:
    base = dict(
        component_id="casa-test/mtg", version="0.1.0", slug="mtg",
        component_checksum="sha256:" + "1" * 64, root_digest="sha256:" + "4" * 64,
        mission="Answer test questions.",
        default_persona_ref="casa/judge@0.1.0", default_persona_checksum="sha256:" + "2" * 64,
        required_config_names=(), required_secret_names=(),
        dependencies=(DependencyResolution(kind="persona", identifier="casa/judge@0.1.0",
                                            digest="sha256:" + "2" * 64, available=True, detail=""),),
        staged_dir=Path("/config/specialists/.staging/x"),
    )
    base.update(overrides)
    return InspectionResult(**base)


def test_identity_is_stable_for_the_same_inputs() -> None:
    a = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg")
    b = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg")
    assert a == b


def test_identity_changes_when_component_checksum_changes() -> None:
    a = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "1" * 64, slug="mtg")
    b = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                  root_digest="sha256:" + "9" * 64, slug="mtg")
    assert a != b


def test_ack_store_is_unacked_until_recorded(tmp_path: Path) -> None:
    store = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    assert store.is_acked(identity) is False
    store.record(identity=identity, component_id="casa-test/mtg", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="mtg")
    assert store.is_acked(identity) is True


def test_ack_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "acks.json"
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    SpecialistInstallAckStore(path=path).record(
        identity=identity, component_id="casa-test/mtg", version="0.1.0",
        component_checksum="sha256:" + "1" * 64, slug="mtg")
    reopened = SpecialistInstallAckStore(path=path)
    assert reopened.is_acked(identity) is True


def test_ack_store_fails_closed_on_a_hand_edited_key(tmp_path: Path) -> None:
    import json

    path = tmp_path / "acks.json"
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    SpecialistInstallAckStore(path=path).record(
        identity=identity, component_id="casa-test/mtg", version="0.1.0",
        component_checksum="sha256:" + "1" * 64, slug="mtg")
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Tamper: change the recorded component_checksum without updating the key.
    list(raw["acks"].values())[0]["component_checksum"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    tampered = SpecialistInstallAckStore(path=path)
    assert tampered.is_acked(identity) is False  # whole-store fail-closed, never partial trust


def test_revoke_removes_an_ack(tmp_path: Path) -> None:
    store = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id="casa-test/mtg", version="0.1.0",
                                         root_digest="sha256:" + "1" * 64, slug="mtg")
    store.record(identity=identity, component_id="casa-test/mtg", version="0.1.0",
                 component_checksum="sha256:" + "1" * 64, slug="mtg")
    assert store.revoke(identity) is True
    assert store.is_acked(identity) is False


def test_render_message_names_slug_and_dependencies() -> None:
    text = render_install_consent_message(_inspection())
    assert "mtg" in text
    assert "casa/judge@0.1.0" in text
