"""#398 release 2 — the agent's own entries inside a role's triggers.yaml.

The subject changed: this file used to test a private, agent-owned
``reminders.yaml`` that only this module wrote. It now tests a writer editing
the OPERATOR's shared trigger file, which changes what must be proven:

* an operator entry must survive every operation byte-for-byte in meaning,
  including a malformed one the module cannot even read;
* ownership comes from ``managed_by`` alone — never the ``reminder-`` prefix,
  the ``date`` type or the ``one_shot`` flag, each of which an operator may
  legitimately author;
* **``remove_entry`` must never refuse for a reason ``past_due`` tolerates.**
  The sweep delivers what ``past_due`` selects and then removes it, so any
  check in one and not the other means the sweep can deliver an entry it cannot
  clean up — and redelivers it every five minutes forever.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import yaml

import reminders

pytestmark = pytest.mark.unit

CEST = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)
OVERDUE = "2026-08-03T08:00:00+02:00"
LATER = "2099-08-03T20:00:00+02:00"

HEARTBEAT = {"name": "heartbeat", "type": "interval", "minutes": 60,
             "channel": "telegram", "prompt": "hb"}


def _write(tmp_path, triggers=(HEARTBEAT,), version=1):
    p = tmp_path / "triggers.yaml"
    p.write_text(yaml.safe_dump({"schema_version": version,
                                 "triggers": list(triggers)},
                                sort_keys=False), encoding="utf-8")
    return str(p)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _names(path):
    return [e.get("name") if isinstance(e, dict) else e
            for e in _read(path)["triggers"]]


def _mine(name="reminder-a1b2c3d4", at=LATER, **over):
    entry = {"name": name, "type": "date", "at": at, "one_shot": True,
             "channel": "telegram", "prompt": 'Send this: "Bins."',
             "managed_by": "agent"}
    entry.update(over)
    return entry


def _operators_lookalike(name="reminder-bins", at=OVERDUE):
    """An entry wearing every mark inference used to read as agent-owned.

    The schema permits exactly this, which is why ownership must be data.
    """
    return {"name": name, "type": "date", "at": at, "one_shot": True,
            "channel": "telegram", "prompt": "operator's own"}


# --- add_entry -------------------------------------------------------------


class TestAddEntry:

    def test_it_appends_and_preserves_the_operator_entries(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike()])
        reminders.add_entry(path, _mine())
        assert _names(path) == ["heartbeat", "reminder-bins",
                                "reminder-a1b2c3d4"]

    def test_every_operator_entry_keeps_its_resolved_meaning(self, tmp_path):
        """The whole point of sharing the file: the operator's configuration
        must mean the same thing afterwards. Compared through the LOADER's own
        view, which is what boot actually reads."""
        import agent_loader
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike()])
        before = agent_loader._read_yaml(path)["triggers"]

        reminders.add_entry(path, _mine())

        after = agent_loader._read_yaml(path)["triggers"]
        assert after[:len(before)] == before
        assert len(after) == len(before) + 1

    def test_it_keeps_an_existing_schema_version(self, tmp_path):
        path = _write(tmp_path, version=1)
        reminders.add_entry(path, _mine())
        assert _read(path)["schema_version"] == 1

    def test_a_first_reminder_creates_the_file_at_version_2(self, tmp_path):
        """``triggers.yaml`` is optional for a resident, so the first reminder
        for a role may have to create it. 2 is what the configurator's own add
        recipe writes, and it keeps a future schema tightening (#402) from
        being boot-fatal on a file this writer created."""
        path = str(tmp_path / "triggers.yaml")
        reminders.add_entry(path, _mine())
        assert _read(path) == {"schema_version": 2, "triggers": [_mine()]}

    def test_it_refuses_an_entry_it_would_not_own(self, tmp_path):
        """``managed_by`` is written HERE or the entry is not ours to manage —
        an unmarked entry would be invisible to the sweep and to cancellation,
        i.e. a reminder that can never be delivered late nor removed."""
        path = _write(tmp_path)
        entry = _mine()
        del entry["managed_by"]
        with pytest.raises(ValueError, match="managed_by"):
            reminders.add_entry(path, entry)
        assert _names(path) == ["heartbeat"]

    def test_it_refuses_a_duplicate_of_an_OPERATOR_name(self, tmp_path):
        """``register_agent`` raises on a duplicate name and that is uncaught
        at boot — a crash loop, not a lost reminder."""
        path = _write(tmp_path)
        with pytest.raises(ValueError, match="already exists"):
            reminders.add_entry(path, _mine(name="heartbeat"))
        assert _names(path) == ["heartbeat"]

    def test_it_refuses_a_placeholder_in_the_reminders_own_text(self, tmp_path):
        """A stored ``${VAR}`` is substituted by the loader at boot, so the
        reminder would not say what the user asked for."""
        path = _write(tmp_path)
        with pytest.raises(ValueError, match=r"\$\{\.\.\.\}"):
            reminders.add_entry(path, _mine(prompt="Send ${HOME}"))
        assert _names(path) == ["heartbeat"]

    def test_a_refusal_writes_nothing_at_all(self, tmp_path):
        """Fail closed: no partial write, no truncated file."""
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike()])
        before = pathlib.Path(path).read_bytes()
        with pytest.raises(ValueError):
            reminders.add_entry(path, _mine(name="heartbeat"))
        assert pathlib.Path(path).read_bytes() == before

    def test_a_schema_violation_surfaces_as_ValueError(self, tmp_path):
        """``agent_loader._validate`` raises ``LoadError``, a direct
        ``Exception`` subclass that ``set_reminder`` does not catch (Sol r2 #2).
        Unfolded it would escape as an unstructured crash instead of the
        ``write_failed`` result the tool promises."""
        path = _write(tmp_path)
        with pytest.raises(ValueError):
            reminders.add_entry(path, {"name": "reminder-x", "type": "date",
                                       "managed_by": "agent"})
        assert _names(path) == ["heartbeat"]

    def test_a_PRE_EXISTING_schema_failure_does_not_block_the_write(
            self, tmp_path):
        """The other arm of Sol r2 #1, which named only ``remove_entry``.

        The running process holds the snapshot it booted with, so the file on
        disk can already be invalid while Casa runs: ``config_git_commit``
        refuses a commit failing the boot-parity check and leaves the working
        tree edited. Refusing then protects nothing — that file already fails to
        load — while making reminders unavailable for the role and naming an
        entry the user never touched.

        Red case: schema-validate the whole candidate document and this raises,
        citing ``half-edited`` — an entry the reminder has nothing to do with.
        """
        import agent_loader
        path = _write(tmp_path, [
            HEARTBEAT,
            # A cron entry missing its schedule: the shape a refused
            # configurator commit leaves behind.
            {"name": "half-edited", "type": "cron", "channel": "telegram",
             "prompt": "x"},
        ])
        assert agent_loader._validate.__name__  # the loader is the authority
        with pytest.raises(agent_loader.LoadError):
            agent_loader._validate(agent_loader._read_yaml(path), "triggers",
                                   path)

        reminders.add_entry(path, _mine())      # must NOT raise

        assert _names(path) == ["heartbeat", "half-edited",
                                "reminder-a1b2c3d4"]

    def test_a_failure_THIS_write_causes_is_still_refused(self, tmp_path):
        """The distinction that keeps the above from being a hole: when the
        prior document was fine, a new complaint is ours and refuses."""
        path = _write(tmp_path)
        with pytest.raises(ValueError, match="schema violation"):
            reminders.add_entry(path, _mine(at=None))
        assert _names(path) == ["heartbeat"]

    def test_an_invalid_entry_is_refused_EVEN_beside_a_pre_existing_defect(
            self, tmp_path):
        """Terra impl r2 — the inverse case, and the hole in the first fix.

        The first attempt at the permissive path compared the whole candidate
        against the whole PRIOR document, so *any* pre-existing complaint waved
        through *any* new one: both documents fail, so "the prior fails too" read
        as "this write is blameless" — while the write was in fact adding a
        second, brand-new boot defect that would outlive the operator fixing the
        first.

        Judging the ADDED ENTRY alone answers the question directly instead.

        Red case: infer blamelessness from whole-document failure and this write
        succeeds, persisting an invalid reminder.
        """
        path = _write(tmp_path, [
            HEARTBEAT,
            {"name": "half-edited", "type": "cron", "channel": "telegram",
             "prompt": "x"},                      # pre-existing defect
        ])
        before = pathlib.Path(path).read_bytes()

        with pytest.raises(ValueError, match="schema violation"):
            reminders.add_entry(path, _mine(at=None))   # invalid: date, no `at`

        assert pathlib.Path(path).read_bytes() == before

    def test_a_pre_existing_TOP_LEVEL_defect_does_refuse(self, tmp_path):
        """The boundary of the rule above, pinned so it is not mistaken for a
        leak (Sol + Terra, impl r3).

        The added entry is judged under the document's REAL top level, because
        ``schema_version`` decides what is legal. So a defect in the top level
        itself is not separable from the judgment and does refuse — unlike a
        defect in a sibling entry, which does not. Deliberate: such a file cannot
        boot either way, so neither refusing nor writing helps the operator, and
        the alternative is validating the entry against a top level the file does
        not actually have.
        """
        import yaml as _yaml
        p = tmp_path / "triggers.yaml"
        p.write_text(_yaml.safe_dump({
            "schema_version": 1, "operator_note": "an unknown root key",
            "triggers": [HEARTBEAT]}, sort_keys=False), encoding="utf-8")
        before = p.read_bytes()

        with pytest.raises(ValueError, match="root"):
            reminders.add_entry(str(p), _mine())

        assert p.read_bytes() == before

    def test_a_VALID_entry_still_lands_beside_a_pre_existing_defect(
            self, tmp_path):
        """And the permissive path must survive the fix above — otherwise the
        cure re-creates the block it was meant to remove."""
        path = _write(tmp_path, [
            HEARTBEAT,
            {"name": "half-edited", "type": "cron", "channel": "telegram",
             "prompt": "x"},
        ])
        reminders.add_entry(path, _mine())
        assert "reminder-a1b2c3d4" in _names(path)

    def test_a_BROKEN_SCHEMA_fails_closed_instead_of_waving_the_write_through(
            self, tmp_path, monkeypatch):
        """The prior-document comparison must not become a validation bypass.

        If validation itself cannot run, treating that as an ordinary
        "this document is invalid" verdict would silently disable the one check
        that stops a boot-breaking file being written. Only a schema VERDICT
        (``LoadError``) is a statement about the document; anything else means
        the check did not happen, and must refuse.

        Red case: fold every exception into a returned complaint and this write
        succeeds with no validation at all.
        """
        import agent_loader
        path = _write(tmp_path)

        def broken(*a, **k):
            raise RuntimeError("schema file is corrupt")

        monkeypatch.setattr(agent_loader, "_validate", broken)
        with pytest.raises(ValueError, match="cannot validate"):
            reminders.add_entry(path, _mine())
        assert _names(path) == ["heartbeat"]

    def test_the_written_entry_loads_back_through_the_real_loader(
            self, tmp_path):
        """A reminder the loader would reject at boot is not durable — and a
        writer validated only against its own idea of the schema is exactly how
        that ships."""
        import agent_loader
        path = _write(tmp_path)
        reminders.add_entry(path, _mine())
        doc = agent_loader._read_yaml(path)
        agent_loader._validate(doc, "triggers", path)
        specs = agent_loader._build_triggers(doc, agent_dir=str(tmp_path))
        assert [s.managed_by for s in specs] == ["", "agent"]


# --- remove_entry ----------------------------------------------------------


class TestRemoveEntry:

    def test_it_removes_only_that_entry(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, _mine()])
        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        assert _names(path) == ["heartbeat"]

    def test_an_absent_name_is_not_found(self, tmp_path):
        path = _write(tmp_path)
        assert reminders.remove_entry(path, "reminder-nope0000") == "not_found"

    def test_a_missing_file_is_not_found(self, tmp_path):
        path = str(tmp_path / "triggers.yaml")
        assert reminders.remove_entry(path, "reminder-nope0000") == "not_found"
        assert not pathlib.Path(path).exists(), "must not create the file"

    def test_an_operator_trigger_is_not_owned_and_is_untouched(self, tmp_path):
        path = _write(tmp_path)
        before = pathlib.Path(path).read_bytes()
        assert reminders.remove_entry(path, "heartbeat") == "not_owned"
        assert pathlib.Path(path).read_bytes() == before

    def test_an_operator_LOOKALIKE_is_not_owned(self, tmp_path):
        """THE pin. This entry carries the reserved prefix, ``type: date`` and
        ``one_shot: true`` — every mark three rounds of #396 findings inferred
        ownership from. Only the absent ``managed_by`` distinguishes it."""
        path = _write(tmp_path, [_operators_lookalike()])
        before = pathlib.Path(path).read_bytes()
        assert reminders.remove_entry(path, "reminder-bins") == "not_owned"
        assert pathlib.Path(path).read_bytes() == before

    def test_an_unrelated_duplicate_name_does_not_block_removal(
            self, tmp_path):
        """Sol r2 #1. A whole-file duplicate check would raise here, and the
        sweep — which has ALREADY delivered — would redeliver every five
        minutes forever. The running process can hold a valid snapshot while
        the on-disk file is invalid: ``config_git_commit`` refuses such a
        commit but leaves the working tree edited.

        Red case: reinstate a whole-file duplicate check and this raises while
        ``past_due`` below still selects the entry.
        """
        path = _write(tmp_path, [
            HEARTBEAT, dict(HEARTBEAT, minutes=30), _mine(at=OVERDUE)])
        assert reminders.past_due(path, NOW), "precondition: sweep would deliver"

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"

        assert _names(path) == ["heartbeat", "heartbeat"], \
            "the operator's duplicates are preserved, not silently deduped"
        assert reminders.past_due(path, NOW) == [], "no redelivery next pass"

    def test_removal_tolerates_everything_past_due_tolerates(self, tmp_path):
        """The module's standing rule, stated as one test. Any state in which
        ``past_due`` yields an entry must be one in which that entry can be
        removed."""
        path = _write(tmp_path, [
            "a bare string, not a mapping",
            {"name": ["not", "a", "string"]},
            dict(HEARTBEAT, one_shot=1),
            _mine(at=OVERDUE),
        ])
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-a1b2c3d4"]
        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        # Every malformed operator entry is still there — skipped by selection,
        # never dropped from the document.
        assert len(_read(path)["triggers"]) == 3


# --- the ${VAR} door -------------------------------------------------------


class TestEnvPlaceholderDoor:
    """``_substitute_env`` runs on the file's TEXT before parsing, and
    ``safe_dump`` re-emits a placeholder-bearing string UNQUOTED — so a ``#``
    in the substituted value starts a comment and truncates the operator's
    value. No emission style avoids it (quoting turns the truncation into a
    boot-fatal parse error), so the writer refuses at the door instead.

    The check is on the RAW TEXT, hence environment-INDEPENDENT. Sol r1 killed
    the alternative — comparing resolved values — because it passes with a
    benign value today and the rewritten file still changes meaning tomorrow.
    """

    # Written as LITERAL TEXT, with the quotes an operator would have authored.
    # Going through ``safe_dump`` here would strip them before the test starts —
    # which is the very transformation under examination.
    AUTHORED = (
        'schema_version: 1\n'
        'triggers:\n'
        '  - name: op-alert\n'
        '    type: cron\n'
        '    schedule: "0 8 * * *"\n'
        '    channel: telegram\n'
        '    prompt: "Send ${DETAIL}"\n'
    )

    def _authored(self, tmp_path, extra=""):
        p = tmp_path / "triggers.yaml"
        p.write_text(self.AUTHORED + extra, encoding="utf-8")
        return str(p)

    def test_add_refuses_and_leaves_the_file_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DETAIL", "bins tonight")   # deliberately BENIGN
        path = self._authored(tmp_path)
        before = pathlib.Path(path).read_bytes()

        with pytest.raises(ValueError, match="interpolation"):
            reminders.add_entry(path, _mine())

        assert pathlib.Path(path).read_bytes() == before

    def test_the_refusal_does_not_depend_on_the_current_value(
            self, tmp_path, monkeypatch):
        """Sol r1's red case. With a benign value a resolved-value comparison
        would PASS and write the prompt unquoted; the damage appears only once
        the value later contains a ``#``. This proves the door closes on the
        text, before any value is consulted."""
        monkeypatch.delenv("DETAIL", raising=False)
        path = self._authored(tmp_path)
        with pytest.raises(ValueError, match="interpolation"):
            reminders.add_entry(path, _mine())

        # And the hazard the door prevents is real. With the value that makes it
        # bite, the AUTHORED file resolves correctly while a safe_dump rewrite
        # of the very same data silently truncates it.
        monkeypatch.setenv("DETAIL", "bins # tonight")
        import agent_loader
        from config import _substitute_env
        assert agent_loader._read_yaml(path)["triggers"][0]["prompt"] == \
            "Send bins # tonight", "quoted in the authored file"
        rewritten = yaml.safe_dump(_read(path), sort_keys=False)
        assert yaml.safe_load(_substitute_env(rewritten))["triggers"][0][
            "prompt"] == "Send bins", "unquoted by safe_dump — TRUNCATED"

    def test_remove_PROCEEDS_rather_than_stranding_a_reminder(
            self, tmp_path, monkeypatch):
        """Terra r1's red case. Refusing here is what creates the permanent
        redelivery loop: the sweep has already dispatched, presence is the
        ledger, so a blocked removal redelivers every five minutes forever.

        Reachable because the operator can add a placeholder entry AFTER a
        reminder is already pending.
        """
        monkeypatch.setenv("DETAIL", "bins # tonight")
        path = self._authored(tmp_path, extra=(
            '  - {name: reminder-a1b2c3d4, type: date, '
            f'at: "{OVERDUE}", one_shot: true, channel: telegram, '
            'prompt: x, managed_by: agent}\n'))
        assert reminders.past_due(path, NOW), "precondition: sweep would deliver"

        assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"
        assert reminders.past_due(path, NOW) == [], "no redelivery next pass"


# --- past_due --------------------------------------------------------------


class TestPastDue:

    def test_only_overdue_agent_owned_date_entries(self, tmp_path):
        path = _write(tmp_path, [
            HEARTBEAT,
            _mine("reminder-old11111", OVERDUE),
            _mine("reminder-new22222", LATER),
            _mine("reminder-rec33333", type="cron", schedule="0 7 * * thu",
                  one_shot=False, at=""),
        ])
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-old11111"]

    def test_an_operator_lookalike_is_never_swept(self, tmp_path):
        """The negative the live probe repeats: a hand-authored
        ``reminder-``-prefixed past-dated one-shot with no ``managed_by`` is
        neither delivered nor removed."""
        path = _write(tmp_path, [_operators_lookalike()])
        assert reminders.past_due(path, NOW) == []
        assert reminders.remove_entry(path, "reminder-bins") == "not_owned"

    def test_an_unparseable_at_is_skipped_not_raised(self, tmp_path):
        path = _write(tmp_path, [_mine("reminder-bad44444", "not-a-time"),
                                 _mine("reminder-old11111", OVERDUE)])
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-old11111"]

    def test_a_date_entry_missing_one_shot_is_still_swept(self, tmp_path):
        """Membership is decided on the type alone. Requiring the flag here
        too would mean an entry lacking it is skipped at registration (past)
        AND by the sweep — silently never delivered."""
        entry = _mine(at=OVERDUE)
        del entry["one_shot"]
        path = _write(tmp_path, [entry])
        assert len(reminders.past_due(path, NOW)) == 1

    def test_a_missing_file_is_empty(self, tmp_path):
        assert reminders.past_due(str(tmp_path / "nope.yaml"), NOW) == []


# --- read failures ---------------------------------------------------------


class TestReadFailures:
    """Every failure folds into ``ValueError`` because readers and writers here
    catch exactly ``(OSError, ValueError)``. An unfolded ``yaml.YAMLError``
    would escape and abort the whole sweep, so later roles' overdue reminders
    would go undelivered."""

    @pytest.mark.parametrize("text", [
        "{{{ not: valid: yaml\n",
        "- a list, not a mapping\n",
        "schema_version: 2\ntriggers: not-a-list\n",
    ])
    def test_a_bad_document_suspends_both_directions(self, tmp_path, text):
        p = tmp_path / "triggers.yaml"
        p.write_text(text, encoding="utf-8")
        path = str(p)

        # None, NOT [] — an empty list would authorise reverse reconciliation
        # to drop every reminder job on one bad read.
        assert reminders.agent_entries(path) is None
        assert reminders.past_due(path, NOW) == []
        assert reminders.existing_names(path) == set()
        # And removal fails the SAME way, so the sweep never delivers something
        # it cannot then clean up.
        with pytest.raises(ValueError):
            reminders.remove_entry(path, "reminder-a1b2c3d4")

    def test_a_document_that_parses_but_cannot_be_RE_EMITTED_still_folds(
            self, tmp_path):
        """Sol impl r1. Parsing tolerance and EMISSION tolerance differ.

        Measured with the pinned PyYAML 6.0.3 at the default recursion limit:
        ~200 levels of nesting parses and dumps; **~400 parses but makes
        ``safe_dump`` raise ``RecursionError``**; ~800 fails to parse at all. So
        there is a real window where ``past_due`` selects an entry that cleanup
        then cannot write back.

        ``RecursionError`` is a ``RuntimeError``, so unfolded it escapes every
        ``except (OSError, ValueError)`` here AND in the sweep — aborting the
        pass after a delivery, which skips every later role and redelivers the
        entry on each subsequent pass.

        Red case: drop ``_emit``'s fold and this raises ``RecursionError``
        instead of ``ValueError``, and the sweep test below aborts.
        """
        deep = "[" * 400 + "]" * 400
        p = tmp_path / "triggers.yaml"
        p.write_text(
            "schema_version: 1\n"
            "triggers:\n"
            f'  - {{name: reminder-a1b2c3d4, type: date, at: "{OVERDUE}", '
            "one_shot: true, channel: telegram, prompt: x, "
            "managed_by: agent}\n"
            f"  - {{name: deep, type: interval, minutes: 1, "
            f"channel: telegram, prompt: {deep}}}\n", encoding="utf-8")
        path = str(p)

        # It genuinely parses, so the sweep WOULD select and deliver it.
        assert [e["name"] for e in reminders.past_due(path, NOW)] == [
            "reminder-a1b2c3d4"]

        # ...and the cleanup failure is a ValueError, inside the contract.
        with pytest.raises(ValueError, match="cannot be re-emitted"):
            reminders.remove_entry(path, "reminder-a1b2c3d4")
        with pytest.raises(ValueError, match="cannot be re-emitted"):
            reminders.add_entry(path, _mine("reminder-bbbb2222"))

    def test_triggers_not_a_list_is_never_coerced_to_empty(self, tmp_path):
        """Coercing it to ``[]`` would silently erase every operator trigger on
        the next write."""
        p = tmp_path / "triggers.yaml"
        p.write_text("schema_version: 2\ntriggers: {a: 1}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not a list"):
            reminders.add_entry(str(p), _mine())
        assert "a: 1" in p.read_text(encoding="utf-8")

    def test_an_alias_is_read_the_way_the_LOADER_reads_it(self, tmp_path):
        """The writer must see exactly the document boot sees, and
        ``agent_loader._read_yaml`` uses plain ``safe_load``, which permits
        aliases. Refusing them here would add no security — the loader parses
        the same file — while making a legitimate file unwritable.
        """
        import agent_loader
        p = tmp_path / "triggers.yaml"
        p.write_text(
            "schema_version: 2\n"
            "triggers:\n"
            "  - &hb {name: heartbeat, type: interval, minutes: 60,\n"
            "         channel: telegram, prompt: hb}\n", encoding="utf-8")
        path = str(p)
        assert reminders.existing_names(path) == \
            {e["name"] for e in agent_loader._read_yaml(path)["triggers"]}


# --- agent_entries ---------------------------------------------------------


class TestAgentEntries:

    def test_it_returns_only_agent_owned_entries(self, tmp_path):
        path = _write(tmp_path, [HEARTBEAT, _operators_lookalike(),
                                 _mine("reminder-aaaa1111")])
        assert [e["name"] for e in reminders.agent_entries(path)] == [
            "reminder-aaaa1111"]

    def test_a_missing_file_is_empty_not_none(self, tmp_path):
        """Absent is a genuine "the agent owns nothing here", which must still
        authorise dropping orphaned jobs. Only an unreadable file is None."""
        assert reminders.agent_entries(str(tmp_path / "nope.yaml")) == []
