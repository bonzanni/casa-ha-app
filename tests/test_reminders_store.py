"""#396 — reminder entries inside a role's reminders.yaml."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

import reminders

CEST = timezone(timedelta(hours=2))

BASE = {
    "schema_version": 1,
    "triggers": [
        {"name": "heartbeat", "type": "interval", "minutes": 60,
         "channel": "telegram", "prompt": "hb"},
    ],
}


def _write(tmp_path, doc=None):
    p = tmp_path / "reminders.yaml"
    p.write_text(yaml.safe_dump(doc if doc is not None else BASE),
                 encoding="utf-8")
    return str(p)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _entry(name="reminder-a1b2c3", at="2026-08-03T08:00:00+02:00"):
    return {"name": name, "type": "date", "at": at, "one_shot": True,
            "channel": "telegram", "prompt": 'Send this: "Bins."'}


def test_append_preserves_existing_triggers(tmp_path):
    path = _write(tmp_path)
    reminders.append_entry(path, _entry())
    doc = _read(path)
    assert [t["name"] for t in doc["triggers"]] == ["heartbeat", "reminder-a1b2c3"]
    assert doc["schema_version"] == 1


def test_append_creates_the_file_when_absent(tmp_path):
    path = str(tmp_path / "reminders.yaml")
    reminders.append_entry(path, _entry())
    doc = _read(path)
    assert doc["schema_version"] == 1
    assert [t["name"] for t in doc["triggers"]] == ["reminder-a1b2c3"]


def test_append_refuses_a_non_reminder_name(tmp_path):
    path = _write(tmp_path)
    with pytest.raises(ValueError):
        reminders.append_entry(path, _entry(name="heartbeat-2"))
    assert [t["name"] for t in _read(path)["triggers"]] == ["heartbeat"]


def test_appended_entry_survives_a_reload_round_trip(tmp_path):
    """The written YAML must load back as the same data — a reminder that
    cannot be re-read at boot is not durable."""
    path = _write(tmp_path)
    reminders.append_entry(path, _entry())
    again = _read(path)["triggers"][1]
    assert again == _entry()


def test_remove_returns_true_and_drops_only_that_entry(tmp_path):
    path = _write(tmp_path)
    reminders.append_entry(path, _entry())
    assert reminders.remove_entry(path, "reminder-a1b2c3") is True
    assert [t["name"] for t in _read(path)["triggers"]] == ["heartbeat"]


def test_remove_absent_is_false_not_an_error(tmp_path):
    path = _write(tmp_path)
    assert reminders.remove_entry(path, "reminder-nope00") is False


def test_remove_refuses_a_non_reminder_name(tmp_path):
    """INV-TRIG-007 red case: the canceller must not be able to delete an
    operator-authored trigger."""
    path = _write(tmp_path)
    with pytest.raises(ValueError):
        reminders.remove_entry(path, "heartbeat")
    assert [t["name"] for t in _read(path)["triggers"]] == ["heartbeat"]


def test_past_due_returns_only_overdue_one_shot_reminders(tmp_path):
    path = _write(tmp_path)
    reminders.append_entry(path, _entry("reminder-old111", "2026-08-03T08:00:00+02:00"))
    reminders.append_entry(path, _entry("reminder-new222", "2026-08-03T20:00:00+02:00"))
    reminders.append_entry(path, {
        "name": "reminder-rec333", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "telegram", "prompt": "x"})
    now = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)
    assert [e["name"] for e in reminders.past_due(path, now)] == ["reminder-old111"]


def test_past_due_ignores_operator_triggers(tmp_path):
    """A cron trigger the operator wrote is never swept, whatever its shape."""
    path = _write(tmp_path, {
        "schema_version": 1,
        "triggers": [{"name": "morning-briefing", "type": "date",
                      "at": "2020-01-01T08:00:00+02:00", "one_shot": True,
                      "channel": "telegram", "prompt": "x"}],
    })
    now = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)
    assert reminders.past_due(path, now) == []


def test_past_due_skips_a_corrupt_entry_but_returns_the_rest(tmp_path):
    path = _write(tmp_path)
    reminders.append_entry(path, _entry("reminder-bad444", "not-a-time"))
    reminders.append_entry(path, _entry("reminder-old111"))
    now = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)
    assert [e["name"] for e in reminders.past_due(path, now)] == ["reminder-old111"]


def test_past_due_on_a_missing_file_is_empty(tmp_path):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)
    assert reminders.past_due(str(tmp_path / "nope.yaml"), now) == []


def test_reminders_path_is_a_separate_agent_owned_file():
    """NOT triggers.yaml: config_sync resolves an edited triggers.yaml as
    "image wins" once the image ships a changed default, which would delete
    every pending reminder on an update."""
    got = reminders.reminders_path("/config/agents", "assistant")
    assert got.endswith("/config/agents/assistant/reminders.yaml")
    assert not got.endswith("triggers.yaml")


def test_malformed_yaml_is_folded_into_valueerror(tmp_path):
    """load_yaml_no_aliases raises yaml.YAMLError, which is NOT a ValueError.
    Unfolded it would escape every `except (OSError, ValueError)` here and
    abort the whole sweep, so later roles' overdue reminders would go
    undelivered."""
    p = tmp_path / "reminders.yaml"
    p.write_text("{{{ not: valid: yaml\n", encoding="utf-8")
    assert reminders.all_entries(str(p)) is None
    assert reminders.past_due(str(p), datetime(2026, 8, 3, 12, 0, tzinfo=CEST)) == []
    assert reminders.existing_names(str(p)) == set()


def test_a_yaml_alias_is_also_folded(tmp_path):
    p = tmp_path / "reminders.yaml"
    p.write_text("a: &x {b: 1}\ntriggers: *x\n", encoding="utf-8")
    assert reminders.all_entries(str(p)) is None


def test_a_non_mapping_list_item_does_not_crash_past_due(tmp_path):
    p = tmp_path / "reminders.yaml"
    p.write_text(yaml.safe_dump({"schema_version": 1, "triggers": [
        "a bare string, not a mapping",
        {"name": "reminder-old111", "type": "date", "one_shot": True,
         "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
         "prompt": "x"},
    ]}), encoding="utf-8")
    got = reminders.past_due(str(p), datetime(2026, 8, 3, 12, 0, tzinfo=CEST))
    assert [e["name"] for e in got] == ["reminder-old111"]


def test_a_non_mapping_entry_is_survivable_by_EVERY_reader_and_writer(tmp_path):
    """Round 10 (both reviewers): guarding one function left its siblings
    raising on the same item — past_due delivered, then remove_entry raised,
    aborting the sweep right after a delivery, so the reminder was redelivered
    every pass and later roles were skipped. Normalising once at the load
    boundary is what makes every consumer safe."""
    p = tmp_path / "reminders.yaml"
    p.write_text(yaml.safe_dump({"schema_version": 1, "triggers": [
        "a bare string, not a mapping",
        {"name": "reminder-old111", "type": "date", "one_shot": True,
         "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
         "prompt": "x"},
    ]}), encoding="utf-8")
    path = str(p)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)

    assert [e["name"] for e in reminders.past_due(path, now)] == ["reminder-old111"]
    assert reminders.existing_names(path) == {"reminder-old111"}
    assert [e["name"] for e in reminders.all_entries(path)] == ["reminder-old111"]
    # The write paths must not raise on it either.
    assert reminders.remove_entry(path, "reminder-old111") is True
    reminders.append_entry(path, {
        "name": "reminder-new222", "type": "date", "one_shot": True,
        "at": "2099-01-01T00:00:00+00:00", "channel": "telegram",
        "prompt": "x"})
    assert reminders.existing_names(path) == {"reminder-new222"}


@pytest.mark.parametrize("bad", [
    "a bare string, not a mapping",
    ["a", "list"],
    42,
    None,
    {"name": ["reminder-bad"]},                 # non-string name
    {"name": {"a": 1}},                         # unhashable name
    {"name": "reminder-bad", "at": 123},        # non-string at
    {"name": "reminder-bad", "at": ["x"]},
    {"name": "reminder-bad", "type": 7},
    {"name": "reminder-bad", "prompt": ["x"]},  # collection prompt
    {"name": "reminder-bad", "channel": 1},
    {"name": "reminder-bad", "schedule": 5},
    {"name": "reminder-bad", "one_shot": 1},    # int is not bool
    {"type": "date", "at": "2026-01-01T00:00:00+00:00"},   # no name at all
])
def test_no_malformed_entry_can_crash_any_consumer(tmp_path, bad):
    """Rounds 10 and 11 were the same class ratcheting through field types —
    non-mapping, then non-string name, then non-string `at`. The boundary now
    checks EVERY field this module reads, so no further variant is reachable.
    A malformed entry must never abort the sweep: that skips later roles and
    redelivers whatever was already sent."""
    good = {"name": "reminder-old111", "type": "date", "one_shot": True,
            "at": "2026-08-03T08:00:00+02:00", "channel": "telegram",
            "prompt": "x"}
    p = tmp_path / "reminders.yaml"
    p.write_text(yaml.safe_dump({"schema_version": 1,
                                 "triggers": [bad, good]}), encoding="utf-8")
    path, now = str(p), datetime(2026, 8, 3, 12, 0, tzinfo=CEST)

    assert [e["name"] for e in reminders.past_due(path, now)] == ["reminder-old111"]
    assert [e["name"] for e in reminders.all_entries(path)] == ["reminder-old111"]
    assert reminders.existing_names(path) == {"reminder-old111"}
    assert reminders.remove_entry(path, "reminder-old111") is True
    reminders.append_entry(path, dict(good, name="reminder-new222"))
    assert reminders.existing_names(path) == {"reminder-new222"}
