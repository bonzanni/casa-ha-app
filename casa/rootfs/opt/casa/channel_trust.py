"""Channel trust attribution for Casa agents.

`channel_trust()` returns the CANONICAL trust token for a channel.
`channel_trust_display()` returns the human-readable form for
rendering inside the <channel_context> system-prompt block.

Trust ordering (highest → lowest):
    internal > authenticated > external-authenticated > household-shared > public

Per-turn AUTHOR identity is NOT here — see :mod:`ingress_identity`, which maps
an ingress ROUTE to the peer that authored its turns. The two are different
axes: this module answers "how much may the agent disclose on this channel?",
that one answers "who wrote this?". A shared bearer secret settles the first and
says nothing about the second (#204).

(A ``user_peer_for_channel`` helper used to live here. It defaulted to
``"nicola"`` for any channel absent from its map, so /invoke and /webhook would
have inherited the operator's identity by omission. Peers are now declared per
route, with no default.)

Future voice-ID upgrade path: a recognised speaker can be promoted
from ``voice_speaker`` to ``nicola`` at the channel layer before
``Agent.handle_message`` — change the voice entries in
:mod:`ingress_identity`.
"""

from __future__ import annotations

_CHANNEL_TRUST_TOKEN: dict[str, str] = {
    "telegram":  "authenticated",
    "voice":     "household-shared",
    "scheduler": "internal",
    # webhook = /invoke + /webhook, gated by the HMAC secret. Operator decision
    # (2026-07-10): the secret IS the trust boundary, so a holder is trusted
    # like the authenticated DM — meets disclosure.yaml's required_trust:
    # authenticated, so the agent may disclose private categories. (Was
    # "external-authenticated", which sat below the bar and made the agent
    # withhold — X2, cross-surface sweep 2026-07-09.)
    "webhook":   "authenticated",
}

_CHANNEL_TRUST_DISPLAY: dict[str, str] = {
    "telegram":  "authenticated (Nicola)",
    "voice":     "household-shared (speaker unauthenticated)",
    "scheduler": "internal (system-initiated)",
    "webhook":   "authenticated (shared secret)",
}


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
