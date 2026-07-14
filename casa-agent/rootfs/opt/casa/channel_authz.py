"""Channel-capability authorization for direct-ingress seams (spec A3).

A resident is reachable on an ingress only if its ``channels:`` list declares
the matching capability token. Unknown ingress names fail closed. ``ha_voice``
is the established declaration token (butler); ``voice`` is the internal
bus/transport name — the mapping below is the single place bridging the two.
"""
from __future__ import annotations

CHANNEL_CAPABILITY: dict[str, str] = {
    "voice": "ha_voice",
    "webhook": "webhook",
}


def agent_allowed_on(ingress: str, cfg) -> bool:
    cap = CHANNEL_CAPABILITY.get(ingress)
    if cap is None:
        return False
    return cap in (getattr(cfg, "channels", None) or [])
