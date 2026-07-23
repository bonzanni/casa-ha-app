"""Task 9: crash-safe bundle-op journal + boot reconciliation with
quarantine semantics (design spec §3.1)."""
from __future__ import annotations

import json

import pytest

import plugin_registry
import specialist_bundle_journal as journal
from plugin_fixtures import owned_entry
from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

pytestmark = pytest.mark.unit


def _registry_doc(entries):
    return {"schema_version": 1, "seeded_defaults": [], "plugins": entries}


def _write_registry(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_registry_doc(entries)), encoding="utf-8")


def _read_registry(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _finance_entry():
    return owned_entry(name="finance.finance", owner="specialist:finance",
                       manifest_name="finance", repo="bonzanni/casa-finance-specialist")


# --------------------------------------------------------------------------
# begin / mark_step / complete lifecycle
# --------------------------------------------------------------------------

def test_begin_writes_journal_with_full_before_state(tmp_path):
    ops_dir = tmp_path / "ops"
    entries = [owned_entry()]
    ack_records = [{"component_id": "c", "version": "1",
                    "component_checksum": "x", "slug": "mtg"}]
    path = journal.begin(
        "install", "mtg",
        before_entries=entries,
        before_tuple_files={"active.yaml": "old-content"},
        ack_records=ack_records,
        receipt_digest="deadbeef",
        ops_dir=ops_dir,
    )
    assert path.parent == ops_dir
    assert journal.JOURNAL_NAME_RE.match(path.name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["op"] == "install"
    assert payload["slug"] == "mtg"
    assert payload["state"] == "in-progress"
    assert payload["before"]["registry_entries"] == entries
    assert payload["before"]["tuple_files"] == {"active.yaml": "old-content"}
    assert payload["before"]["ack_records"] == ack_records
    assert payload["receipt_digest"] == "deadbeef"
    assert payload["steps_done"] == []


def test_mark_step_appends(tmp_path):
    ops_dir = tmp_path / "ops"
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    journal.mark_step(path, "cas_published")
    journal.mark_step(path, "registry_swapped")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["steps_done"] == ["cas_published", "registry_swapped"]


def test_complete_writes_complete_state_then_unlinks(tmp_path):
    ops_dir = tmp_path / "ops"
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    journal.complete(path)
    assert not path.exists()


# --------------------------------------------------------------------------
# reconcile_boot: no-op cases
# --------------------------------------------------------------------------

def test_reconcile_boot_noop_absent_ops_dir(tmp_path):
    actions = journal.reconcile_boot(
        ops_dir=tmp_path / "nope", registry_path=tmp_path / "registry.json",
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")
    assert actions == []
    assert journal.last_boot_reconcile_actions == []


def test_reconcile_boot_noop_empty_ops_dir(tmp_path):
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=tmp_path / "registry.json",
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")
    assert actions == []


def test_reconcile_boot_skips_preexisting_quarantined_file(tmp_path):
    """Idempotency: a file already renamed .quarantined by an earlier boot is
    left completely untouched on the next boot."""
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    q = ops_dir / "mtg.deadbeef.json.quarantined"
    q.write_text("not even valid json", encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == []
    assert q.read_text(encoding="utf-8") == "not even valid json"


# --------------------------------------------------------------------------
# reconcile_boot: in-progress journal -> rollback
# --------------------------------------------------------------------------

def test_reconcile_boot_rolls_back_inprogress_journal(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"

    before_entry = owned_entry()
    # "current" mid-mutation registry state: the before-entry is already gone.
    _write_registry(registry_path, [])

    slug_dir = specialists_dir / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.yaml").write_text("mid-mutation", encoding="utf-8")
    (slug_dir / "desired.yaml").write_text("mid-mutation-desired", encoding="utf-8")

    ack_record = {"component_id": "casa-mtg-specialist", "version": "0.2.0",
                  "component_checksum": "root-digest", "slug": "mtg", "ts": 1}

    journal.begin(
        "install", "mtg",
        before_entries=[before_entry],
        before_tuple_files={"active.yaml": "pre-mutation", "desired.yaml": None},
        ack_records=[ack_record],
        ops_dir=ops_dir,
    )

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=specialists_dir, acks_path=acks_path)

    assert actions == [{"slug": "mtg", "action": "rolled_back"}]
    assert list(ops_dir.iterdir()) == []

    doc = _read_registry(registry_path)
    assert doc["plugins"] == [before_entry]

    assert (slug_dir / "active.yaml").read_text(encoding="utf-8") == "pre-mutation"
    assert not (slug_dir / "desired.yaml").exists()

    identity = install_consent_identity(
        component_id=ack_record["component_id"], version=ack_record["version"],
        root_digest=ack_record["component_checksum"], slug=ack_record["slug"])
    restored = SpecialistInstallAckStore(acks_path).get(identity)
    assert restored is not None and restored["slug"] == "mtg"


def test_reconcile_boot_idempotent_second_run_is_noop(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"
    _write_registry(registry_path, [])
    journal.begin("install", "mtg", before_entries=[owned_entry()],
                 before_tuple_files={}, ack_records=[], ops_dir=ops_dir)

    first = journal.reconcile_boot(ops_dir=ops_dir, registry_path=registry_path,
                                    specialists_dir=specialists_dir, acks_path=acks_path)
    assert first == [{"slug": "mtg", "action": "rolled_back"}]

    second = journal.reconcile_boot(ops_dir=ops_dir, registry_path=registry_path,
                                     specialists_dir=specialists_dir, acks_path=acks_path)
    assert second == []


def test_reconcile_boot_stashes_actions_on_module_attribute(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    journal.begin("install", "mtg", before_entries=[owned_entry()],
                 before_tuple_files={}, ack_records=[], ops_dir=ops_dir)
    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")
    assert journal.last_boot_reconcile_actions == actions
    assert actions == [{"slug": "mtg", "action": "rolled_back"}]


# --------------------------------------------------------------------------
# reconcile_boot: state == "complete" crash window -> prune WITHOUT rollback
# --------------------------------------------------------------------------

def test_reconcile_boot_prunes_complete_journal_without_rollback(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"

    # "after" state: the op finished — the owned entry IS now present.
    after_entry = owned_entry()
    _write_registry(registry_path, [after_entry])

    path = journal.begin(
        "install", "mtg",
        before_entries=[],   # before the op there was NO owned entry
        before_tuple_files={},
        ack_records=[],
        ops_dir=ops_dir,
    )
    # Simulate the crash window: state flipped to "complete" but the unlink
    # never happened.
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"] = "complete"
    path.write_text(json.dumps(payload), encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=specialists_dir, acks_path=acks_path)

    assert actions == [{"slug": "mtg", "action": "pruned_complete"}]
    assert list(ops_dir.iterdir()) == []
    # NOT rolled back — the after-state (owned entry present) survives.
    doc = _read_registry(registry_path)
    assert doc["plugins"] == [after_entry]


# --------------------------------------------------------------------------
# reconcile_boot: filename matches, payload corrupt/invalid -> quarantine(slug)
# --------------------------------------------------------------------------

def test_reconcile_boot_corrupt_json_quarantines_exactly_that_slug(tmp_path):
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry(), _finance_entry()])
    bad = ops_dir / f"mtg.{'a' * 32}.json"
    bad.write_text("{not json", encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert not bad.exists()
    assert bad.with_name(bad.name + ".quarantined").exists()

    doc = _read_registry(registry_path)
    names = [e["name"] for e in doc["plugins"]]
    assert "mtg.mtg" not in names
    assert "finance.finance" in names
    assert doc["quarantined_bundles"] == ["mtg"]


def test_reconcile_boot_payload_slug_mismatch_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["slug"] = "other"
    path.write_text(json.dumps(payload), encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()
    assert _read_registry(registry_path)["quarantined_bundles"] == ["mtg"]


def test_reconcile_boot_malformed_before_shape_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["before"] = ["not", "a", "mapping"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()


def test_reconcile_boot_tuple_files_key_outside_fixed_set_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[],
                         before_tuple_files={"unexpected.yaml": "x"},
                         ack_records=[], ops_dir=ops_dir)

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()


def test_reconcile_boot_traversal_tuple_key_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[],
                         before_tuple_files={"../evil.yaml": "x"},
                         ack_records=[], ops_dir=ops_dir)

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()


def test_reconcile_boot_ack_restore_failure_quarantines_slug(tmp_path):
    """A structurally-valid-looking ack record (a dict — passes the strict
    shape check) missing required keys blows up deep inside
    SpecialistInstallAckStore.restore_records (KeyError). reconcile_boot must
    catch that and quarantine the slug rather than crash boot (per Task 7:
    restore_records is atomic — nothing persisted on raise)."""
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"
    _write_registry(registry_path, [])

    bad_ack = {"slug": "mtg"}   # missing component_id/version/component_checksum
    journal.begin(
        "install", "mtg",
        before_entries=[owned_entry()],
        before_tuple_files={},
        ack_records=[bad_ack],
        ops_dir=ops_dir,
    )

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=specialists_dir, acks_path=acks_path)

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    doc = _read_registry(registry_path)
    assert doc["plugins"] == []   # quarantine cleans up the partial rollback
    assert doc["quarantined_bundles"] == ["mtg"]
    remaining = list(ops_dir.iterdir())
    assert len(remaining) == 1 and remaining[0].name.endswith(".quarantined")


# --------------------------------------------------------------------------
# reconcile_boot: unparseable filename -> quarantine_all (never delete)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("filename", ["garbage.json", "noslug", "mtg.nothex.json"])
def test_reconcile_boot_unparseable_filename_quarantines_all(tmp_path, filename):
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry(), _finance_entry()])
    bad = ops_dir / filename
    bad.write_text("whatever bytes", encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": None, "action": "quarantine_all"}]
    assert not bad.exists()
    assert (ops_dir / f"{filename}.quarantined").exists()

    doc = _read_registry(registry_path)
    assert doc["plugins"] == []
    assert set(doc["quarantined_bundles"]) == {"mtg", "finance"}


# --------------------------------------------------------------------------
# quarantine / quarantine_all direct unit tests
# --------------------------------------------------------------------------

def test_quarantine_removes_owned_entries_and_flags_slug(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry(), _finance_entry()])
    journal.quarantine("mtg", registry_path=registry_path)
    doc = _read_registry(registry_path)
    assert [e["name"] for e in doc["plugins"]] == ["finance.finance"]
    assert doc["quarantined_bundles"] == ["mtg"]


def test_quarantine_is_idempotent(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    journal.quarantine("mtg", registry_path=registry_path)
    journal.quarantine("mtg", registry_path=registry_path)
    doc = _read_registry(registry_path)
    assert doc["quarantined_bundles"] == ["mtg"]


def test_quarantine_all_removes_every_owned_entry_keeps_unowned(tmp_path):
    registry_path = tmp_path / "registry.json"
    unowned = {
        "name": "gmail",
        "source": {"type": "github", "repo": "o/r3", "ref": "v1",
                   "revision": "git:" + "b" * 40, "subdir": ""},
        "artifact_id": plugin_registry.compute_artifact_id(
            repo="o/r3", revision="git:" + "b" * 40, subdir="", name="gmail"),
        "version": "1.0.0", "targets": ["resident:tina"],
    }
    _write_registry(registry_path, [owned_entry(), _finance_entry(), unowned])
    journal.quarantine_all(registry_path=registry_path)
    doc = _read_registry(registry_path)
    assert [e["name"] for e in doc["plugins"]] == ["gmail"]
    assert set(doc["quarantined_bundles"]) == {"mtg", "finance"}


# --------------------------------------------------------------------------
# BundleTxn.rollback_disk: direct unit test with non-default paths
# --------------------------------------------------------------------------

def test_bundletxn_rollback_disk_restores_registry_tuple_files_and_acks(tmp_path):
    registry_path = tmp_path / "reg" / "registry.json"
    specialists_dir = tmp_path / "spec"
    acks_path = tmp_path / "acks" / "acks.json"
    _write_registry(registry_path, [])

    slug_dir = specialists_dir / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.yaml").write_text("mid-mutation", encoding="utf-8")

    before_entry = owned_entry()
    ack_record = {"component_id": "casa-mtg-specialist", "version": "0.2.0",
                  "component_checksum": "root-digest", "slug": "mtg", "ts": 1}

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[before_entry],
        before_tuple_files={"active.yaml": "pre-mutation"},
        ack_records=[ack_record],
        registry_path=registry_path,
        specialists_dir=specialists_dir,
        acks_path=acks_path,
    )
    txn.rollback_disk()

    doc = _read_registry(registry_path)
    assert doc["plugins"] == [before_entry]
    assert (slug_dir / "active.yaml").read_text(encoding="utf-8") == "pre-mutation"

    identity = install_consent_identity(
        component_id=ack_record["component_id"], version=ack_record["version"],
        root_digest=ack_record["component_checksum"], slug=ack_record["slug"])
    assert SpecialistInstallAckStore(acks_path).get(identity) is not None


def test_bundletxn_rollback_disk_deletes_files_recorded_as_absent(tmp_path):
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "spec"
    acks_path = tmp_path / "acks.json"
    _write_registry(registry_path, [])

    slug_dir = specialists_dir / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "desired.yaml").write_text("created-mid-mutation", encoding="utf-8")

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[],
        before_tuple_files={"desired.yaml": None},
        ack_records=[],
        registry_path=registry_path,
        specialists_dir=specialists_dir,
        acks_path=acks_path,
    )
    txn.rollback_disk()

    assert not (slug_dir / "desired.yaml").exists()
