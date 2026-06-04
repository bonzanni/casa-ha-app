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


async def test_retain_cold_session_telegram(monkeypatch):
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(session_saver, "get_session_messages",
                        lambda sid, directory: [_Msg("user", "hi")])
    sem = _Sem()
    await session_saver.retain_cold_session(
        sid="s1", role="assistant", directory="/tmp",
        user_peer="nicola", channel="telegram", semantic_memory=sem,
    )
    assert sem.retained == [("casa", [["friends"]])]


async def test_retain_cold_session_voice_noop():
    sem = _Sem()
    await session_saver.retain_cold_session(
        sid="s1", role="butler", directory="/tmp",
        user_peer="voice_speaker", channel="voice", semantic_memory=sem,
    )
    assert sem.retained == []  # recall-only channel never retains


def test_gap_branch_does_not_await_save_session():
    src = inspect.getsource(agent_mod.Agent._process)
    assert "await save_session(" not in src        # gap save is no longer inline/blocking
    assert "_spawn_cold_retain(" in src            # it schedules a background retain


def test_agent_has_background_tasks_set():
    # __init__ initialises the tracking set
    src = inspect.getsource(agent_mod.Agent.__init__)
    assert "_bg_tasks" in src
