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
    assert out["type"] == "cron"
    assert out["schedule"] == "0 7 * * thu"


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
    # `at` is retained purely as the anchor (start_date); recurrence is driven
    # by the cron fields, so the offset never shifts the series.
    assert reminders.derive_schedule(summer, "weekly")["at"].startswith(
        "2026-08-06T07:00:00")


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


def test_recurring_retains_its_anchor():
    """Sol r1 #3: without the anchor, "every Thursday from the 20th" set on
    the 3rd fires on the 6th and 13th — occurrences the user never asked
    for."""
    at = datetime(2026, 8, 20, 7, 0, tzinfo=CEST)
    out = reminders.derive_schedule(at, "weekly")
    assert out["schedule"] == "0 7 * * thu"
    assert reminders.parse_at(out["at"]) == at


def test_recurring_anchor_rounds_UP_to_a_whole_minute():
    """Round 3 (both reviewers): cron has minute resolution and fires at
    second zero. Rounding DOWN was wrong twice over — the truncated minute may
    already have passed (pushing the first occurrence a whole period late) and
    the series would fire seconds earlier than the user was told. Rounding up
    keeps the first occurrence in the future AND the promise exact."""
    at = datetime(2026, 8, 4, 7, 5, 30, 123456, tzinfo=CEST)
    out = reminders.derive_schedule(at, "daily")
    anchor = reminders.parse_at(out["at"])
    assert anchor == datetime(2026, 8, 4, 7, 6, tzinfo=CEST)
    assert anchor.second == 0 and anchor.microsecond == 0
    # The schedule is derived from the ROUNDED value, so the two agree.
    assert out["schedule"] == "6 7 * * *"


def test_a_minute_aligned_anchor_is_untouched():
    at = datetime(2026, 8, 4, 7, 5, tzinfo=CEST)
    out = reminders.derive_schedule(at, "daily")
    assert out["schedule"] == "5 7 * * *"
    assert reminders.parse_at(out["at"]) == at


def test_rounding_up_can_roll_the_weekday_and_stay_consistent():
    """23:59:30 on a Thursday rounds to Friday 00:00 — the derived weekday
    must follow the rounded value, not the original."""
    at = datetime(2026, 8, 6, 23, 59, 30, tzinfo=CEST)   # Thursday
    out = reminders.derive_schedule(at, "weekly")
    anchor = reminders.parse_at(out["at"])
    assert anchor == datetime(2026, 8, 7, 0, 0, tzinfo=CEST)   # Friday
    assert out["schedule"] == "0 0 * * fri"


def test_one_shot_keeps_its_exact_instant():
    """A date trigger is scheduled at the instant itself, not by a cron
    expression, so its seconds must be preserved."""
    at = datetime(2026, 8, 4, 7, 5, 30, tzinfo=CEST)
    out = reminders.derive_schedule(at, "none")
    assert reminders.parse_at(out["at"]) == at


def test_new_name_avoids_names_already_taken():
    """Sol r2 #3: a collision fails registration on a duplicate job id, and
    the caller's rollback then deletes the PRE-EXISTING reminder too."""
    taken = {f"reminder-{i:08x}" for i in range(50)}
    for _ in range(100):
        assert reminders.new_reminder_name(taken) not in taken


def test_new_name_raises_rather_than_returning_a_collision(monkeypatch):
    monkeypatch.setattr(reminders.secrets, "token_hex", lambda n: "deadbeef")
    with pytest.raises(ValueError):
        reminders.new_reminder_name({"reminder-deadbeef"})


def test_existing_names_reads_the_store(tmp_path):
    path = str(tmp_path / "reminders.yaml")
    reminders.append_entry(path, {
        "name": "reminder-aaaaaaaa", "type": "date", "one_shot": True,
        "at": "2099-01-01T00:00:00+00:00", "channel": "telegram",
        "prompt": "x"})
    assert reminders.existing_names(path) == {"reminder-aaaaaaaa"}
    assert reminders.existing_names(str(tmp_path / "nope.yaml")) == set()


def test_monthly_past_the_28th_means_end_of_month():
    """A literal day>28 does not exist in every month and cron SKIPS the
    months it is missing from: "monthly on the 31st" would fire only 7 times
    a year. A reminder that misses five months is not monthly."""
    for day in (29, 30, 31):
        at = datetime(2026, 1, day, 9, 0, tzinfo=CEST)
        assert reminders.derive_schedule(at, "monthly")["schedule"] == "0 9 last * *"


def test_monthly_on_or_before_the_28th_is_literal():
    at = datetime(2026, 1, 28, 9, 0, tzinfo=CEST)
    assert reminders.derive_schedule(at, "monthly")["schedule"] == "0 9 28 * *"


def test_end_of_month_schedule_actually_fires_every_month():
    """Pin the behaviour against APScheduler itself, not just the string."""
    from apscheduler.triggers.cron import CronTrigger

    at = datetime(2026, 1, 31, 9, 0, tzinfo=CEST)
    minute, hour, day, month, dow = (
        reminders.derive_schedule(at, "monthly")["schedule"].split())
    trig = CronTrigger(minute=minute, hour=hour, day=day, month=month,
                       day_of_week=dow, timezone=CEST)

    seen, now = [], datetime(2026, 1, 1, tzinfo=CEST)
    for _ in range(6):
        nxt = trig.get_next_fire_time(None, now)
        seen.append(nxt.strftime("%Y-%m-%d"))
        now = nxt + timedelta(seconds=1)

    assert seen == ["2026-01-31", "2026-02-28", "2026-03-31",
                    "2026-04-30", "2026-05-31", "2026-06-30"]
