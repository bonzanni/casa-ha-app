"""Engagement primitive — Tier 2 Specialist interactive mode + (Plan 3+) Tier 3 Executors.

Symmetric with :mod:`specialist_registry`. Owns:
- EngagementRecord (one in-flight engagement)
- EngagementRegistry (in-memory dict + ``/data/engagements.json`` tombstone)
- Idle sweep (fires ``idle_detected`` bus events + session-suspends live clients)
- Orphan recovery (startup: load tombstone; records remain dormant until
  next user turn in their topic)

See docs/superpowers/specs/2026-04-22-3.5-plan2-engagement-primitive-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass
class EngagementRecord:
    """One in-flight engagement.

    ``kind`` = "specialist" for Tier 2 interactive mode; "executor" for Tier 3
    (Plan 3+). ``role_or_type`` is the specialist role (e.g. "finance") or the
    executor type (e.g. "configurator").

    ``status`` transitions:
      active ──first idle sweep past 24h──▶ idle
      idle    ──next user turn──▶ active
      active  ──emit_completion / /complete──▶ completed
      active  ──/cancel / cancel_engagement──▶ cancelled
      active  ──resume twice failed / sweep orphan──▶ error
    """

    id: str
    kind: str
    role_or_type: str
    driver: str
    status: str
    topic_id: int | None
    started_at: float
    last_user_turn_ts: float
    last_idle_reminder_ts: float
    completed_at: float | None
    sdk_session_id: str | None
    origin: dict[str, Any]
    task: str
