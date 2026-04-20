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
        assert reg.get("alex") is None

    async def test_missing_dir_is_noop(self, tmp_path):
        from executor_registry import ExecutorRegistry

        reg = ExecutorRegistry(str(tmp_path / "does_not_exist"),
                               tombstone_path=str(tmp_path / "del.json"))
        # Must NOT raise.
        reg.load()
        assert reg.get("alex") is None

    async def test_loads_enabled_executor(self, tmp_path):
        from executor_registry import ExecutorRegistry

        executors = tmp_path / "executors"
        executors.mkdir()
        _write(str(executors / "alex.yaml"),
               "name: Alex\nrole: alex\nmodel: sonnet\npersonality: a\n"
               "enabled: true\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        cfg = reg.get("alex")
        assert cfg is not None
        assert cfg.role == "alex"
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
        _write(str(executors / "alex.yaml"),
               "name: Alex\nrole: alex\nmodel: sonnet\npersonality: a\n"
               "enabled: false\n"
               "memory:\n  token_budget: 0\n"
               "session:\n  strategy: ephemeral\n  idle_timeout: 0\n")
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.INFO):
            reg.load()
        # Not registered for delegation dispatch.
        assert reg.get("alex") is None
        # One-line disabled-log present.
        assert any(
            "disabled" in r.message.lower() and "alex" in r.message.lower()
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
            id="d-1", agent="alex", started_at=1.0,
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
            id="d-2", agent="alex", started_at=1.0,
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
            id="d-3", agent="alex", started_at=1.0,
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
            delegation_id="d-1", agent="alex", status="ok",
        )
        assert c.text == ""
        assert c.kind == ""
        assert c.message == ""
        assert c.origin == {}
        assert c.elapsed_s == 0.0

    async def test_full_ok(self):
        from executor_registry import DelegationComplete

        c = DelegationComplete(
            delegation_id="d-1", agent="alex", status="ok",
            text="result text",
            origin={"role": "assistant"},
            elapsed_s=2.5,
        )
        assert c.status == "ok"
        assert c.text == "result text"
