"""#398 release 2 — provenance is an entry FIELD, and reminders.yaml is gone.

Replaces ``test_agent_loader_reminders.py``, whose subject was the two-file
merge this release deletes. ``reminders.yaml`` existed for one reason: a
previous analysis found that ``config_sync`` would erase a locally-added
``triggers.yaml`` entry on any update whose shipped default also changed.
Release 1 (#398) made that file reconcile PER ENTRY, so the risk is gone and
the separate file has no purpose left.

Two things carry over from the old suite and are the reason this file exists:

* **Every test goes through the REAL ``load_agent_from_dir``.** The first
  version of the old suite called ``_validate``/``_build_triggers`` directly,
  which bypasses ``_check_file_set`` — so it passed while the tier allowlist
  was wrong, and the real system would have crash-looped. The same shortcut
  would hide the allowlist assertions below.
* **The anti-inference pin.** The schema PERMITS an operator to author
  ``name: reminder-bins / type: date / one_shot: true``. Ownership must come
  from ``managed_by`` alone, never from the name, the type or the flag —
  inferring it is what produced three rounds of findings in #396.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest
import yaml

pytestmark = pytest.mark.unit


def _helpers():
    try:
        from tests.test_agent_loader import _policies_file, _seed_resident
    except ImportError:
        from test_agent_loader import _policies_file, _seed_resident
    return _seed_resident, _policies_file


def _reminder(name="reminder-a1b2c3d4", **over):
    entry = {"name": name, "type": "date", "one_shot": True,
             "at": "2099-08-03T08:00:00+02:00", "channel": "telegram",
             "prompt": 'Send this exact message via telegram: "Bins."'}
    entry.update(over)
    return entry


HEARTBEAT = {"name": "heartbeat", "type": "interval", "minutes": 60,
             "channel": "telegram", "prompt": "hb"}


def _seed(tmp_path, role="assistant", triggers=None, stray=None):
    """Build a resident directory; return its path. Does NOT load."""
    seed_resident, _ = _helpers()
    d = seed_resident(tmp_path / "agents", role=role)
    if triggers is not None:
        pathlib.Path(d / "triggers.yaml").write_text(
            yaml.safe_dump(triggers), encoding="utf-8")
    if stray is not None:
        name, text = stray
        pathlib.Path(d / name).write_text(textwrap.dedent(text),
                                          encoding="utf-8")
    return d


def _load(tmp_path, **kw):
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    _, policies_file = _helpers()
    d = _seed(tmp_path, **kw)
    policies = load_policies(str(policies_file(tmp_path / "policies")))
    return load_agent_from_dir(str(d), policies=policies)


def _doc(*entries, version=2):
    return {"schema_version": version, "triggers": list(entries)}


# --- provenance is data ----------------------------------------------------


class TestManagedByIsData:

    def test_the_field_reaches_the_spec(self, tmp_path):
        cfg = _load(tmp_path, triggers=_doc(
            HEARTBEAT, _reminder(managed_by="agent")))
        by_name = {t.name: t for t in cfg.triggers}
        assert by_name["reminder-a1b2c3d4"].managed_by == "agent"

    def test_an_operator_trigger_is_not_agent_owned(self, tmp_path):
        cfg = _load(tmp_path, triggers=_doc(HEARTBEAT))
        assert [t.managed_by for t in cfg.triggers] == [""]

    def test_an_operator_authored_dated_one_shot_is_not_agent_owned(
            self, tmp_path):
        """THE pin. This entry wears every mark that inference used to read as
        agent-owned — the reserved name prefix, ``type: date``,
        ``one_shot: true`` — and the schema permits an operator to author
        exactly it. If ownership were derived from any of those, the sweep
        would deliver it and the reminder tools could delete it.
        """
        cfg = _load(tmp_path, triggers=_doc(_reminder(name="reminder-bins")))
        assert cfg.triggers[0].managed_by == ""

    def test_reminders_and_operator_triggers_coexist_in_one_file(
            self, tmp_path):
        """The whole point of the release: one file, ownership per entry."""
        cfg = _load(tmp_path, triggers=_doc(
            HEARTBEAT,
            _reminder(name="reminder-bins"),                    # operator's
            _reminder(name="reminder-aaaa1111", managed_by="agent"),
        ))
        assert {t.name: t.managed_by for t in cfg.triggers} == {
            "heartbeat": "", "reminder-bins": "",
            "reminder-aaaa1111": "agent",
        }

    def test_from_reminder_store_is_gone(self):
        """Deleted, not aliased: a shim would let a missed call site keep
        working while meaning the wrong thing."""
        from config import TriggerSpec

        assert not hasattr(TriggerSpec(name="x", type="cron"),
                           "from_reminder_store")


# --- reminders.yaml does not exist ----------------------------------------


class TestRemindersYamlIsErased:
    """No fold, no fallback, no allowlist entry, no schema mapping.

    There is deliberately no migration: the wipe (``/config/agents`` removed,
    uninstall destroying ``/data``) lands BEFORE this version is deployed, so
    no install can be holding the file.
    """

    def test_it_is_on_no_tier_list(self):
        from agent_loader import TIER_FILES

        for tier, rules in TIER_FILES.items():
            for kind, names in rules.items():
                assert "reminders.yaml" not in names, (
                    f"reminders.yaml still listed as {kind} for {tier}")

    def test_it_has_no_schema_mapping(self):
        from agent_loader import _SCHEMA_BY_FILENAME

        assert "reminders.yaml" not in _SCHEMA_BY_FILENAME

    def test_a_resident_still_holding_one_fails_to_load(self, tmp_path):
        """**This failure is why the wipe must precede the deploy.**

        ``_check_file_set`` computes ``unknown = on_disk - (required |
        optional)`` and raises, and a resident that fails to load stops boot.
        So an install that still has ``reminders.yaml`` crash-loops. That is an
        accepted consequence of erasing the concept, NOT an oversight — and
        this test is where a future reader who drops the wipe step from the
        runbook finds out.
        """
        from agent_loader import LoadError

        with pytest.raises(LoadError, match="unknown file"):
            _load(tmp_path, triggers=_doc(HEARTBEAT), stray=(
                "reminders.yaml",
                """\
                schema_version: 1
                triggers: []
                """,
            ))

    def test_the_shipped_defaults_do_not_contain_one(self):
        assert not list(pathlib.Path(
            "casa/rootfs/opt/casa/defaults/agents").rglob("reminders.yaml"))

    def test_no_source_module_NAMES_it(self):
        """The filename is unreachable as a path — no module builds it.

        Deliberately scans string LITERALS via ``ast`` rather than the raw file
        text. The property that matters is that no code can open, list or match
        the file; prose that explains why it used to exist is valuable history
        and several modules legitimately carry it (``config.py``'s
        ``managed_by`` comment is the clearest case). A substring scan over the
        source text conflates the two and would push a future author to delete
        the explanation to get green.
        """
        import ast

        root = pathlib.Path("casa/rootfs/opt/casa")
        offenders = []
        for p in root.rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and "reminders.yaml" in node.value):
                    offenders.append(f"{p}:{node.lineno}")
        assert offenders == []
