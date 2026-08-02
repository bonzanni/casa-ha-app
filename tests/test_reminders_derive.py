"""#396 — reminder name generation and schedule derivation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import reminders

CEST = timezone(timedelta(hours=2))


def test_name_is_prefixed_and_unique():
    a, b = reminders.new_reminder_name(), reminders.new_reminder_name()
    assert a.startswith("reminder-") and b.startswith("reminder-")
    assert a != b
    assert reminders.is_reminder_name(a)
    assert not reminders.is_reminder_name("heartbeat")
    assert not reminders.is_reminder_name("")


def test_generated_name_matches_the_schema_name_pattern():
    import re
    pattern = re.compile(r"^(?!plg-)[a-zA-Z0-9_-]+$")
    for _ in range(20):
        assert pattern.match(reminders.new_reminder_name())


def test_none_yields_a_date_trigger_keeping_the_offset():
    at = datetime(2026, 8, 3, 8, 0, tzinfo=CEST)
    out = reminders.derive_schedule(at, "none")
    assert out["type"] == "date"
    assert reminders.parse_at(out["at"]) == at


def test_weekly_yields_a_day_name_not_a_number():
    # 2026-08-06 is a Thursday. A NUMBER here would reintroduce #343.
    at = datetime(2026, 8, 6, 7, 0, tzinfo=CEST)
    out = reminders.derive_schedule(at, "weekly")
    assert out == {"type": "cron", "schedule": "0 7 * * thu"}


def test_daily_weekdays_monthly():
    at = datetime(2026, 8, 6, 7, 5, tzinfo=CEST)
    assert reminders.derive_schedule(at, "daily")["schedule"] == "5 7 * * *"
    assert reminders.derive_schedule(at, "weekdays")["schedule"] == "5 7 * * mon-fri"
    assert reminders.derive_schedule(at, "monthly")["schedule"] == "5 7 6 * *"


def test_every_weekday_maps_to_its_own_name():
    # 2026-08-03 is a Monday; walk a full week.
    expected = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for offset, name in enumerate(expected):
        at = datetime(2026, 8, 3 + offset, 9, 0, tzinfo=CEST)
        assert reminders.derive_schedule(at, "weekly")["schedule"].endswith(
            f" {name}"), f"day {offset} should be {name}"


def test_recurring_discards_the_supplied_offset():
    """DST (spec 7.1): a summer +02:00 request must persist wall-clock fields
    only, so the same reminder fires at 07:00 LOCAL in winter. Persisting the
    offset would shift it by an hour across the transition."""
    summer = datetime(2026, 8, 6, 7, 0, tzinfo=timezone(timedelta(hours=2)))
    winter = datetime(2026, 1, 8, 7, 0, tzinfo=timezone(timedelta(hours=1)))
    assert reminders.derive_schedule(summer, "weekly")["schedule"] == "0 7 * * thu"
    assert reminders.derive_schedule(winter, "weekly")["schedule"] == "0 7 * * thu"
    # And nothing offset-shaped survives into the persisted fields.
    assert "at" not in reminders.derive_schedule(summer, "weekly")


def test_unknown_repeat_rejected():
    at = datetime(2026, 8, 6, 7, 0, tzinfo=CEST)
    with pytest.raises(ValueError):
        reminders.derive_schedule(at, "fortnightly")


def test_parse_at_requires_an_offset():
    with pytest.raises(ValueError):
        reminders.parse_at("2026-08-03T08:00:00")


def test_parse_at_rejects_garbage():
    with pytest.raises(ValueError):
        reminders.parse_at("next tuesday")
    with pytest.raises(ValueError):
        reminders.parse_at("")
