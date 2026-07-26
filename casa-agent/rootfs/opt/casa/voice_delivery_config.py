"""Bounds for proactive voice-job delivery.

v0.125.0 (#228): these were three add-on options
(``voice_route_freshness_seconds``, ``voice_job_delivery_ttl_seconds``,
``voice_job_route_cap``) read from env. They are internal tuning constants —
an operator has no basis on which to choose a route-freshness grace period or
a per-route job cap — so they are now fixed here. The dataclass survives
because it is the boot snapshot every delivery site reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass


logger = logging.getLogger(__name__)

#: Grace period in which an authenticated, background-capable route may
#: briefly disconnect while Casa still accepts specialist work for it.
ROUTE_FRESHNESS_S = 60
#: Maximum time a completed background voice result is retained for delivery.
#: A specialist may request a shorter privacy expiry; the shorter value wins.
DELIVERY_TTL_S = 900
#: Maximum active-or-ready specialist jobs held for one voice route.
ROUTE_CAP = 5


@dataclass(frozen=True)
class VoiceDeliveryConfig:
    route_freshness_s: int
    delivery_ttl_s: int
    route_cap: int


def load_voice_delivery_config() -> VoiceDeliveryConfig:
    """One immutable boot snapshot of the delivery bounds."""
    config = VoiceDeliveryConfig(
        route_freshness_s=ROUTE_FRESHNESS_S,
        delivery_ttl_s=DELIVERY_TTL_S,
        route_cap=ROUTE_CAP,
    )
    logger.info(
        "voice_delivery_config route_freshness_s=%d ttl_s=%d route_cap=%d",
        config.route_freshness_s,
        config.delivery_ttl_s,
        config.route_cap,
    )
    return config


__all__ = ["VoiceDeliveryConfig", "load_voice_delivery_config"]
