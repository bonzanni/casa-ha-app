"""Channel trust attribution for Casa agents.

Maps ingress channels to (a) the Honcho peer that owns the user's
utterances on that surface, (b) a human-readable trust descriptor
injected into the agent's system prompt.

Future voice-ID upgrade path: a recognised speaker can be promoted
from ``voice_speaker`` to ``nicola`` at the channel layer before
``Agent.handle_message`` — no change needed below.
"""

from __future__ import annotations

_USER_PEER_BY_CHANNEL: dict[str, str] = {
    "voice": "voice_speaker",
}

_CHANNEL_TRUST: dict[str, str] = {
    "telegram": "authenticated (Nicola)",
    "voice": "household-shared (speaker unauthenticated)",
    "scheduler": "internal (system-initiated)",
    "webhook": "external (authenticated by shared secret)",
}


def user_peer_for_channel(channel: str) -> str:
    """Return the Honcho peer that owns user-text from *channel*.

    Authenticated channels attribute to ``nicola`` (default).
    The shared voice mic attributes to ``voice_speaker`` to keep
    unauthenticated utterances out of Nicola's peer card.
    """
    return _USER_PEER_BY_CHANNEL.get(channel, "nicola")


def channel_trust(channel: str) -> str:
    """Return a human-readable trust descriptor for *channel*.

    Rendered inside the agent's ``<channel_context>`` block so the
    personality baseline can condition disclosure on it.
    """
    return _CHANNEL_TRUST.get(channel, "unknown")
