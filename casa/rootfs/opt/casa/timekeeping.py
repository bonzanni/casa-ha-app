"""Single source of truth for the app's timezone.

Read from ``CASA_TZ`` env var, else ``TZ`` env var (which HA OS sets),
else Europe/Amsterdam as the final fallback. Used by APScheduler
(so cron wall-clock means local time) and by ``Agent._process``
(for the ``<current_time>`` block in the composed system prompt).

If the resolved name is not a known IANA zone, log a warning and fall
back to ``Europe/Amsterdam`` rather than raising. ``ZoneInfoNotFoundError``
is not cached by ``@lru_cache``, so without this guard a typo'd ``casa_tz``
add-on option would crash every turn.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FALLBACK_TZ = "Europe/Amsterdam"

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def resolve_tz() -> ZoneInfo:
    tz_name = (
        os.environ.get("CASA_TZ")
        or os.environ.get("TZ")
        or _FALLBACK_TZ
    )
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "resolve_tz: %r is not a known IANA timezone; "
            "falling back to %r. Fix the casa_tz add-on option to silence "
            "this warning.", tz_name, _FALLBACK_TZ,
        )
        return ZoneInfo(_FALLBACK_TZ)
