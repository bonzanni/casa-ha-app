"""Tests for executor_registry.py — Tier 2 loader + registry."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _write(path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# TestLoader
# ---------------------------------------------------------------------------


class TestLoader:
    async def test_empty_dir_loads_nothing(self, tmp_path):
        from executor_registry import ExecutorRegistry

        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        assert reg.get("finance") is None

    async def test_missing_dir_is_noop(self, tmp_path):
        from executor_registry import ExecutorRegistry

        reg = ExecutorRegistry(str(tmp_path / "does_not_exist"),
                               tombstone_path=str(tmp_path / "del.json"))
        # Must NOT raise.
        reg.load()
        assert reg.get("finance") is None

    async def test_loads_enabled_executor(self, tmp_path):
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "finance.yaml"),
               "name: Alex\nrole: finance\nmodel: sonnet\npersonality: a\n"
               "enabled: true\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        cfg = reg.get("finance")
        assert cfg is not None
        assert cfg.role == "finance"
        assert cfg.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# TestValidation — Tier-2-shape rejection
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_rejects_non_empty_channels(self, tmp_path, caplog):
        import logging
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "bogus.yaml"),
               "name: B\nrole: bogus\nmodel: sonnet\npersonality: b\n"
               "channels: [telegram]\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("bogus") is None
        assert any(
            "channels" in r.message.lower() for r in caplog.records
        )

    async def test_rejects_non_zero_token_budget(self, tmp_path, caplog):
        import logging
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "rich.yaml"),
               "name: R\nrole: rich\nmodel: sonnet\npersonality: r\n"
               "memory:\n  token_budget: 1000\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("rich") is None
        assert any(
            "token_budget" in r.message for r in caplog.records
        )

    async def test_rejects_non_ephemeral_session(self, tmp_path, caplog):
        import logging
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "sticky.yaml"),
               "name: S\nrole: sticky\nmodel: sonnet\npersonality: s\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: persistent\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("sticky") is None

    async def test_rejects_scopes_owned(self, tmp_path, caplog):
        import logging
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "owner.yaml"),
               "name: O\nrole: owner\nmodel: sonnet\npersonality: o\n"
               "memory:\n  token_budget: 0\n  scopes_owned: [finance]\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("owner") is None
        assert any(
            "scopes_owned" in r.message for r in caplog.records
        )

    async def test_malformed_yaml_skipped(self, tmp_path, caplog):
        import logging
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "broken.yaml"), "not: valid: yaml: here\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("broken") is None


# ---------------------------------------------------------------------------
# TestEnabledFiltering
# ---------------------------------------------------------------------------


class TestEnabledFiltering:
    async def test_disabled_executor_parsed_but_skipped(
        self, tmp_path, caplog,
    ):
        import logging
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "finance.yaml"),
               "name: Alex\nrole: finance\nmodel: sonnet\npersonality: a\n"
               "enabled: false\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.INFO):
            reg.load()
        # Not registered for delegation dispatch.
        assert reg.get("finance") is None
        # One-line disabled-log present.
        assert any(
            "disabled" in r.message.lower() and "finance" in r.message.lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# TestDelegationLifecycle (in-memory only — tombstone in Task 5)
# ---------------------------------------------------------------------------


class TestDelegationLifecycle:
    def _make_registry(self, tmp_path):
        from executor_registry import ExecutorRegistry
        return ExecutorRegistry(str(tmp_path / "executors"),
                                tombstone_path=str(tmp_path / "del.json"))

    async def test_register_then_complete_removes_record(self, tmp_path):
        from executor_registry import DelegationRecord

        reg = self._make_registry(tmp_path)
        rec = DelegationRecord(
            id="d-1", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "x", "cid": "c1", "user_text": "hi"},
        )
        await reg.register_delegation(rec)
        assert reg.has_delegation("d-1")
        await reg.complete_delegation("d-1")
        assert not reg.has_delegation("d-1")

    async def test_fail_delegation_removes_record(self, tmp_path):
        from executor_registry import DelegationRecord

        reg = self._make_registry(tmp_path)
        rec = DelegationRecord(
            id="d-2", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "x", "cid": "c1", "user_text": "hi"},
        )
        await reg.register_delegation(rec)
        await reg.fail_delegation("d-2", RuntimeError("boom"))
        assert not reg.has_delegation("d-2")

    async def test_cancel_delegation_removes_record(self, tmp_path):
        from executor_registry import DelegationRecord

        reg = self._make_registry(tmp_path)
        rec = DelegationRecord(
            id="d-3", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "x", "cid": "c1", "user_text": "hi"},
        )
        await reg.register_delegation(rec)
        await reg.cancel_delegation("d-3")
        assert not reg.has_delegation("d-3")

    async def test_terminal_calls_are_idempotent(self, tmp_path):
        """complete/fail/cancel on a non-existent id must not raise."""
        reg = self._make_registry(tmp_path)
        await reg.complete_delegation("missing")
        await reg.fail_delegation("missing", RuntimeError("x"))
        await reg.cancel_delegation("missing")


# ---------------------------------------------------------------------------
# TestDelegationComplete dataclass shape
# ---------------------------------------------------------------------------


class TestDelegationCompleteShape:
    async def test_defaults(self):
        from executor_registry import DelegationComplete

        c = DelegationComplete(
            delegation_id="d-1", agent="finance", status="ok",
        )
        assert c.text == ""
        assert c.kind == ""
        assert c.message == ""
        assert c.origin == {}
        assert c.elapsed_s == 0.0

    async def test_full_ok(self):
        from executor_registry import DelegationComplete

        c = DelegationComplete(
            delegation_id="d-1", agent="finance", status="ok",
            text="result text",
            origin={"role": "assistant"},
            elapsed_s=2.5,
        )
        assert c.status == "ok"
        assert c.text == "result text"


# ---------------------------------------------------------------------------
# TestTombstone — /data/delegations.json round-trip
# ---------------------------------------------------------------------------


class TestTombstone:
    async def test_register_writes_file(self, tmp_path):
        from executor_registry import DelegationRecord, ExecutorRegistry

        tomb = tmp_path / "del.json"
        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tomb))
        rec = DelegationRecord(
            id="d-1", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "x", "cid": "c1", "user_text": "hi"},
        )
        await reg.register_delegation(rec)

        import json
        data = json.loads(tomb.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "d-1"
        assert data[0]["agent"] == "finance"
        assert data[0]["origin"]["channel"] == "telegram"

    async def test_complete_removes_from_file(self, tmp_path):
        from executor_registry import DelegationRecord, ExecutorRegistry

        tomb = tmp_path / "del.json"
        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tomb))
        rec = DelegationRecord(
            id="d-1", agent="finance", started_at=1.0, origin={},
        )
        await reg.register_delegation(rec)
        await reg.complete_delegation("d-1")

        import json
        data = json.loads(tomb.read_text())
        assert data == []

    async def test_multiple_in_flight(self, tmp_path):
        from executor_registry import DelegationRecord, ExecutorRegistry

        tomb = tmp_path / "del.json"
        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tomb))
        for i in range(3):
            await reg.register_delegation(DelegationRecord(
                id=f"d-{i}", agent="finance", started_at=1.0, origin={},
            ))
        import json
        data = json.loads(tomb.read_text())
        assert {row["id"] for row in data} == {"d-0", "d-1", "d-2"}
        await reg.complete_delegation("d-1")
        data = json.loads(tomb.read_text())
        assert {row["id"] for row in data} == {"d-0", "d-2"}


# ---------------------------------------------------------------------------
# TestOrphanRecovery
# ---------------------------------------------------------------------------


class TestOrphanRecovery:
    async def test_orphans_from_disk_returns_records(self, tmp_path):
        from executor_registry import DelegationRecord, ExecutorRegistry

        tomb = tmp_path / "del.json"
        # Pre-populate as if a prior process left records behind.
        import json
        tomb.write_text(json.dumps([
            {
                "id": "orphan-1", "agent": "finance", "started_at": 100.0,
                "origin": {"role": "assistant", "channel": "telegram",
                           "chat_id": "x", "cid": "c1", "user_text": "hi"},
            },
            {
                "id": "orphan-2", "agent": "finance", "started_at": 101.0,
                "origin": {"role": "assistant", "channel": "telegram",
                           "chat_id": "y", "cid": "c2", "user_text": "hey"},
            },
        ]))
        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tomb))
        orphans = reg.orphans_from_disk()
        assert [o.id for o in orphans] == ["orphan-1", "orphan-2"]
        # File is truncated after read.
        assert json.loads(tomb.read_text()) == []

    async def test_orphans_from_disk_missing_file_is_empty(self, tmp_path):
        from executor_registry import ExecutorRegistry

        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tmp_path / "del.json"))
        assert reg.orphans_from_disk() == []

    async def test_orphans_from_disk_corrupt_logs_and_truncates(
        self, tmp_path, caplog,
    ):
        import logging
        from executor_registry import ExecutorRegistry

        tomb = tmp_path / "del.json"
        tomb.write_text("{not json at all")
        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tomb))
        with caplog.at_level(logging.ERROR):
            orphans = reg.orphans_from_disk()
        assert orphans == []
        # File truncated to empty list on corruption.
        import json
        assert json.loads(tomb.read_text()) == []
        assert any(
            "corrupt" in r.message.lower() or "could not" in r.message.lower()
            for r in caplog.records
        )

    async def test_orphans_from_disk_non_list_logs_and_truncates(
        self, tmp_path, caplog,
    ):
        """Valid JSON but not an array → ERROR log, truncate, return []."""
        import json
        import logging
        from executor_registry import ExecutorRegistry

        tomb = tmp_path / "del.json"
        tomb.write_text(json.dumps({"not": "a list"}))
        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=str(tomb))
        with caplog.at_level(logging.ERROR):
            orphans = reg.orphans_from_disk()
        assert orphans == []
        assert json.loads(tomb.read_text()) == []
        assert any(
            "not a JSON array" in r.message
            for r in caplog.records
        )

    async def test_register_on_disk_failure_logs_warning(
        self, tmp_path, caplog,
    ):
        """If the tombstone write fails, the in-memory delegation is
        still registered — the worst-case is missed orphan recovery."""
        import logging
        from executor_registry import DelegationRecord, ExecutorRegistry

        # Non-writable path — parent directory does not exist and we
        # refuse to create it, to force a write failure.
        bad_path = str(tmp_path / "nonexistent" / "subdir" / "del.json")
        reg = ExecutorRegistry(str(tmp_path / "executors"),
                               tombstone_path=bad_path)
        rec = DelegationRecord(
            id="d-1", agent="finance", started_at=1.0, origin={},
        )
        with caplog.at_level(logging.WARNING):
            await reg.register_delegation(rec)
        assert reg.has_delegation("d-1")
        assert any(
            "tombstone" in r.message.lower() or "delegation" in r.message.lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# TestSummaryLog (Phase 3.4)
# ---------------------------------------------------------------------------


class TestSummaryLog:
    async def test_summary_line_present_with_mixed_enabled_disabled(
        self, tmp_path, caplog,
    ):
        import logging
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        # One enabled executor.
        _write(str(executors / "foo.yaml"),
               "name: Foo\nrole: foo\nmodel: sonnet\npersonality: a\n"
               "enabled: true\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        # One disabled executor.
        _write(str(executors / "bar.yaml"),
               "name: Bar\nrole: bar\nmodel: sonnet\npersonality: a\n"
               "enabled: false\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")

        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.INFO):
            reg.load()

        summary = [r for r in caplog.records
                   if r.message.startswith("Executors:")]
        assert len(summary) == 1, (
            f"expected exactly one 'Executors:' summary line, "
            f"got {len(summary)}: {[r.message for r in summary]}"
        )
        msg = summary[0].message
        assert "'foo'" in msg
        assert "'bar'" in msg
        # Enabled vs disabled positioning — the helper wraps each set in
        # its own bracketed list so we can distinguish them.
        enabled_idx = msg.find("enabled=")
        disabled_idx = msg.find("disabled=")
        assert 0 <= enabled_idx < disabled_idx
        # 'foo' sits in the enabled segment; 'bar' in the disabled segment.
        assert msg.find("'foo'") < disabled_idx
        assert msg.find("'bar'") > disabled_idx

    async def test_summary_line_empty_when_dir_missing(
        self, tmp_path, caplog,
    ):
        import logging
        from executor_registry import ExecutorRegistry

        reg = ExecutorRegistry(str(tmp_path / "does_not_exist"),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.INFO):
            reg.load()

        summary = [r for r in caplog.records
                   if r.message.startswith("Executors:")]
        # Missing dir is a no-op (existing test_missing_dir_is_noop); the
        # summary line is still emitted so operator visibility is uniform.
        assert len(summary) == 1
        msg = summary[0].message
        assert "enabled=[]" in msg
        assert "disabled=[]" in msg
