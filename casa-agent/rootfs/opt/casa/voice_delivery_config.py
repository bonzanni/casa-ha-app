"""Validated operator bounds for proactive voice-job delivery."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceDeliveryConfig:
    route_freshness_s: int
    delivery_ttl_s: int
    route_cap: int


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def load_voice_delivery_config() -> VoiceDeliveryConfig:
    """Read one immutable, defence-in-depth-clamped boot snapshot."""
    config = VoiceDeliveryConfig(
        route_freshness_s=_bounded_env_int(
            "VOICE_ROUTE_FRESHNESS_SECONDS", 60, 0, 300,
        ),
        delivery_ttl_s=_bounded_env_int(
            "VOICE_JOB_DELIVERY_TTL_SECONDS", 900, 30, 3600,
        ),
        route_cap=_bounded_env_int("VOICE_JOB_ROUTE_CAP", 5, 1, 20),
    )
    logger.info(
        "voice_delivery_config route_freshness_s=%d ttl_s=%d route_cap=%d",
        config.route_freshness_s,
        config.delivery_ttl_s,
        config.route_cap,
    )
    return config


__all__ = ["VoiceDeliveryConfig", "load_voice_delivery_config"]
