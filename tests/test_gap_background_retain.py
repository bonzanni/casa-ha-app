# tests/test_gap_background_retain.py
"""Gap-triggered cold-session retain is claim-free, backgroundable, voice-safe."""
import inspect
import pytest

import session_saver
import agent as agent_mod

pytestmark = [pytest.mark.unit]


class _Msg:
    def __init__(self, mtype, text):
        self.type = mtype
        self.message = {"role": mtype, "content": text}


class _Sem:
    def __init__(self):
        self.retained = []

    async def retain(self, bank, items, *, async_=True):
        self.retained.append((bank, [i["tags"] for i in items]))


async def fake_classify(c):
    return "friends"


def _snapshot(*, with_provenance: bool):
    """Build a SessionEntrySnapshot the reduced retain_cold_session consumes."""
    from agent import snapshot_session_entry
    from speaker_provenance import provenance_mapping
    from session_reg_helpers import STUB_SPEAKER_PROV, STUB_USER_PROV
    entry = {"agent": "resident:assistant", "sdk_session_id": "s1"}
    if with_provenance:
        entry["speaker_provenance"] = provenance_mapping(STUB_SPEAKER_PROV)
        entry["user_provenance"] = provenance_mapping(STUB_USER_PROV)
    return snapshot_session_entry(entry)


async def test_retain_cold_session_telegram(monkeypatch):
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    sem = _Sem()
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=True), directory="/tmp",
        channel="telegram", semantic_memory=sem,
    )
    # One retain to the shared bank; the single user turn is tier-tagged "friends".
    assert len(sem.retained) == 1
    bank, tagsets = sem.retained[0]
    assert bank == "casa"
    assert tagsets[0][0] == "friends"                       # tier tag first
    assert any(t.startswith("casa-source-") for t in tagsets[0])  # + provenance tag


async def test_retain_cold_session_voice_noop():
    sem = _Sem()
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=False), directory="/tmp",
        channel="voice", semantic_memory=sem,
    )
    assert sem.retained == []  # recall-only channel never retains


async def test_retain_cold_session_no_provenance_noop(monkeypatch):
    """A legacy/corrupt snapshot with no usable provenance retains NOTHING —
    memory is never written with invented authorship."""
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    sem = _Sem()
    await session_saver.retain_cold_session(
        _snapshot(with_provenance=False), directory="/tmp",
        channel="telegram", semantic_memory=sem,
    )
    assert sem.retained == []


def test_gap_branch_does_not_await_save_session():
    src = inspect.getsource(agent_mod.Agent._process)
    assert "await save_session(" not in src        # gap save is no longer inline/blocking
    assert "_spawn_cold_retain(" in src            # it schedules a background retain


def test_agent_has_background_tasks_set():
    # __init__ initialises the tracking set
    src = inspect.getsource(agent_mod.Agent.__init__)
    assert "_bg_tasks" in src
