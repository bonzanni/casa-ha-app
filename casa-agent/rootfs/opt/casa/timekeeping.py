"""Single source of truth for the app's timezone.

Read from ``CASA_TZ`` env var, else ``TZ`` env var (which HA OS sets),
else Europe/Amsterdam as the final fallback. Used by APScheduler
(so cron wall-clock means local time) and by ``Agent._process``
(for the ``<current_time>`` block in the composed system prompt).
"""

from __future__ import annotations

import os
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def resolve_tz() -> ZoneInfo:
    tz_name = (
        os.environ.get("CASA_TZ")
        or os.environ.get("TZ")
        or "Europe/Amsterdam"
    )
    return ZoneInfo(tz_name)
