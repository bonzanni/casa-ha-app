"""#396 — reminders.yaml is accepted and merged by the REAL loader.

Round 2 lesson: the first version of this suite reimplemented the merge by
calling ``_validate``/``_build_triggers`` directly. That shortcut bypassed
``_check_file_set``, which rejects any file not on the tier allowlist — so it
passed while ``reminders.yaml`` was still absent from that list, and the real
system would have crash-looped on the first reminder ever set. Every test here
now goes through ``load_agent_from_dir``.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest
import yaml

pytestmark = pytest.mark.unit

DEFAULTS = pathlib.Path("casa/rootfs/opt/casa/defaults/agents/assistant")


def _helpers():
    try:
        from tests.test_agent_loader import _policies_file, _seed_resident
    except ImportError:
        from test_agent_loader import _policies_file, _seed_resident
    return _seed_resident, _policies_file


def _write(path, text):
    pathlib.Path(path).write_text(textwrap.dedent(text), encoding="utf-8")


def _reminder(name="reminder-a1b2c3d4"):
    return {"name": name, "type": "date", "one_shot": True,
            "at": "2099-08-03T08:00:00+02:00", "channel": "telegram",
            "prompt": 'Send this exact message via telegram: "Bins."'}


def _load(tmp_path, role="assistant", reminders=None, triggers=None):
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    seed_resident, policies_file = _helpers()
    d = seed_resident(tmp_path / "agents", role=role)
    if triggers is not None:
        _write(d / "triggers.yaml", "")
        pathlib.Path(d / "triggers.yaml").write_text(
            yaml.safe_dump(triggers), encoding="utf-8")
    if reminders is not None:
        pathlib.Path(d / "reminders.yaml").write_text(
            yaml.safe_dump(reminders), encoding="utf-8")
    policies = load_policies(str(policies_file(tmp_path / "policies")))
    return load_agent_from_dir(str(d), policies=policies)


TRIGGERS = {
    "schema_version": 1,
    "triggers": [{"name": "heartbeat", "type": "interval", "minutes": 60,
                  "channel": "telegram", "prompt": "hb"}],
}


def test_a_resident_with_a_reminders_file_loads(tmp_path):
    """P0 (both reviewers, round 2): without reminders.yaml on the tier
    allowlist, _check_file_set rejects the whole resident the moment the first
    reminder exists — an add-on boot crash loop, not a degraded reminder."""
    cfg = _load(tmp_path, triggers=TRIGGERS,
                reminders={"schema_version": 1, "triggers": [_reminder()]})
    assert [t.name for t in cfg.triggers] == ["heartbeat", "reminder-a1b2c3d4"]


def test_reminder_date_fields_survive_the_real_load(tmp_path):
    cfg = _load(tmp_path, triggers=TRIGGERS,
                reminders={"schema_version": 1, "triggers": [_reminder()]})
    spec = [t for t in cfg.triggers if t.name == "reminder-a1b2c3d4"][0]
    assert spec.type == "date"
    assert spec.one_shot is True
    assert spec.at == "2099-08-03T08:00:00+02:00"
    assert "Bins." in spec.prompt


def test_reminders_load_without_any_triggers_file(tmp_path):
    """A fresh install has no triggers.yaml edits; the first reminder must
    still load on its own."""
    cfg = _load(tmp_path,
                reminders={"schema_version": 1, "triggers": [_reminder()]})
    assert [t.name for t in cfg.triggers] == ["reminder-a1b2c3d4"]


def test_absent_reminders_file_is_fine(tmp_path):
    cfg = _load(tmp_path, triggers=TRIGGERS)
    assert [t.name for t in cfg.triggers] == ["heartbeat"]


def test_an_invalid_reminders_file_is_rejected(tmp_path):
    """It must be schema-validated like any other agent file, not trusted
    because an agent wrote it."""
    from agent_loader import LoadError

    with pytest.raises(LoadError):
        _load(tmp_path, triggers=TRIGGERS, reminders={
            "schema_version": 1,
            "triggers": [{"name": "reminder-a1b2c3d4", "type": "date",
                          "channel": "telegram", "prompt": "x"}],  # no at
        })


def test_a_colliding_reminder_is_dropped_not_fatal(tmp_path):
    """register_agent raises on duplicate names and boot does not catch it
    (#338), so a collision across the two files would be a crash loop."""
    cfg = _load(tmp_path, triggers={
        "schema_version": 1,
        "triggers": [{"name": "reminder-a1b2c3d4", "type": "cron",
                      "schedule": "0 7 * * *", "channel": "telegram",
                      "prompt": "operator wrote this"}],
    }, reminders={"schema_version": 1, "triggers": [_reminder()]})

    names = [t.name for t in cfg.triggers]
    assert names == ["reminder-a1b2c3d4"]
    # The operator's entry is the one that survived.
    assert cfg.triggers[0].type == "cron"


def test_the_shipped_default_has_no_reminders_file():
    """Reminders must NOT be in the defaults tree — that is what makes
    config_sync adopt the live file instead of overwriting it on update."""
    assert not (DEFAULTS / "reminders.yaml").exists()
