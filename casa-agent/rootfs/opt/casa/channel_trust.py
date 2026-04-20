"""Channel trust attribution for Casa agents.

`channel_trust()` returns the CANONICAL trust token used by
scope_registry for the (trust × scope.minimum_trust) filter.
`channel_trust_display()` returns the human-readable form for
rendering inside the <channel_context> system-prompt block.

Trust ordering (highest → lowest):
    internal > authenticated > external-authenticated > household-shared > public

Future voice-ID upgrade path: a recognised speaker can be promoted
from ``voice_speaker`` to ``nicola`` at the channel layer before
``Agent.handle_message`` — no change needed below.
"""

from __future__ import annotations

_USER_PEER_BY_CHANNEL: dict[str, str] = {
    "voice": "voice_speaker",
}

_CHANNEL_TRUST_TOKEN: dict[str, str] = {
    "telegram":  "authenticated",
    "voice":     "household-shared",
    "scheduler": "internal",
    "webhook":   "external-authenticated",
}

_CHANNEL_TRUST_DISPLAY: dict[str, str] = {
    "telegram":  "authenticated (Nicola)",
    "voice":     "household-shared (speaker unauthenticated)",
    "scheduler": "internal (system-initiated)",
    "webhook":   "external (authenticated by shared secret)",
}


def user_peer_for_channel(channel: str) -> str:
    """Return the Honcho peer that owns user-text from *channel*."""
    return _USER_PEER_BY_CHANNEL.get(channel, "nicola")


def channel_trust(channel: str) -> str:
    """Return the canonical trust token for *channel*.

    Token strings: `internal`, `authenticated`, `external-authenticated`,
    `household-shared`, `public`. Unknown channels fall back to `public`
    (most restrictive).
    """
    return _CHANNEL_TRUST_TOKEN.get(channel, "public")


def channel_trust_display(channel: str) -> str:
    """Return a human-readable trust descriptor for *channel*.

    Rendered inside the agent's ``<channel_context>`` block so the
    personality baseline can reason about disclosure.
    """
    return _CHANNEL_TRUST_DISPLAY.get(channel, "unknown")
