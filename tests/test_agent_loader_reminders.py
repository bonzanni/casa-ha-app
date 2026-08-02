"""#396 — reminders.yaml is merged into the trigger list at load time."""

from __future__ import annotations

import pathlib

import pytest
import yaml

pytestmark = pytest.mark.unit

DEFAULTS = pathlib.Path("casa/rootfs/opt/casa/defaults/agents/assistant")

TRIGGERS = {
    "schema_version": 1,
    "triggers": [
        {"name": "heartbeat", "type": "interval", "minutes": 60,
         "channel": "telegram", "prompt": "hb"},
    ],
}


def _reminder(name="reminder-a1b2c3"):
    return {"name": name, "type": "date", "one_shot": True,
            "at": "2099-08-03T08:00:00+02:00", "channel": "telegram",
            "prompt": 'Send this exact message via telegram: "Bins."'}


def _agent_dir(tmp_path, reminders=None, triggers=None):
    """A minimal resident dir carrying only the files under test."""
    d = tmp_path / "assistant"
    d.mkdir()
    (d / "triggers.yaml").write_text(
        yaml.safe_dump(triggers if triggers is not None else TRIGGERS),
        encoding="utf-8")
    if reminders is not None:
        (d / "reminders.yaml").write_text(
            yaml.safe_dump(reminders), encoding="utf-8")
    return d


def _load_triggers(agent_dir):
    """Exercise the same two-file merge load_agent_from_dir performs."""
    import agent_loader

    cfg = type("Cfg", (), {"triggers": []})()
    trig_path = agent_dir / "triggers.yaml"
    data = yaml.safe_load(trig_path.read_text())
    agent_loader._validate(data, "triggers", str(trig_path))
    cfg.triggers = agent_loader._build_triggers(data, agent_dir=str(agent_dir))

    rem_path = agent_dir / "reminders.yaml"
    if rem_path.exists():
        rem = yaml.safe_load(rem_path.read_text())
        agent_loader._validate(rem, "triggers", str(rem_path))
        existing = {t.name for t in cfg.triggers}
        for spec in agent_loader._build_triggers(rem, agent_dir=str(agent_dir)):
            if spec.name in existing:
                continue
            existing.add(spec.name)
            cfg.triggers = list(cfg.triggers) + [spec]
    return cfg.triggers


def test_reminders_are_merged_into_the_trigger_list(tmp_path):
    d = _agent_dir(tmp_path, reminders={"schema_version": 1,
                                        "triggers": [_reminder()]})
    names = [t.name for t in _load_triggers(d)]
    assert names == ["heartbeat", "reminder-a1b2c3"]


def test_a_reminder_keeps_its_date_fields(tmp_path):
    d = _agent_dir(tmp_path, reminders={"schema_version": 1,
                                        "triggers": [_reminder()]})
    spec = [t for t in _load_triggers(d) if t.name == "reminder-a1b2c3"][0]
    assert spec.type == "date"
    assert spec.one_shot is True
    assert spec.at == "2099-08-03T08:00:00+02:00"
    assert "Bins." in spec.prompt


def test_absent_reminders_file_is_fine(tmp_path):
    d = _agent_dir(tmp_path)
    assert [t.name for t in _load_triggers(d)] == ["heartbeat"]


def test_a_colliding_reminder_is_dropped_not_fatal(tmp_path):
    """register_agent raises on duplicate names and boot does not catch it
    (#338), so a collision across the two files would be a crash loop. The
    operator's triggers.yaml wins."""
    d = _agent_dir(tmp_path, reminders={
        "schema_version": 1,
        "triggers": [_reminder(), _reminder()],   # same name twice
    })
    names = [t.name for t in _load_triggers(d)]
    assert names == ["heartbeat", "reminder-a1b2c3"]


def test_the_shipped_default_has_no_reminders_file():
    """Reminders must NOT be in the defaults tree — that is what makes
    config_sync adopt the live file instead of overwriting it on update."""
    assert not (DEFAULTS / "reminders.yaml").exists()
