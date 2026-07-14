"""Spec §4.3 — voice stt_start prewarm: no session ensure, no overlay call.

Under the tiered-memory model voice clearance is 'friends', which is below the
'private' threshold required to push the overlay.  The overlay warm via
profile() was therefore dead work and has been removed.

v0.80.0 (spec A2): stt_start ALSO no longer ensure()s a VoiceSession — the
frame carries no agent_role, and ensure() now requires one (role-scoped pool
keying, so two residents on one device can't collide on a session). Pool
registration happens lazily on the utterance frame instead, which does carry
agent_role. The real end-to-end contract (stt_start touches nothing in the
pool; the pool entry appears only after an utterance) is exercised against
the actual `_ws_handler` in test_voice_channel_ws.py::TestWSTurn — this file
keeps only the structural check below (no lightweight fake-pool simulation
here duplicates that, since it wouldn't be driving real channel.py code).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


async def test_no_prewarm_method_on_voice_channel():
    """VoiceChannel must not expose a _prewarm method after the cleanup."""
    from channels.voice.channel import VoiceChannel
    assert not hasattr(VoiceChannel, "_prewarm"), (
        "_prewarm was not removed; the obsolete overlay prewarm is still present"
    )
