"""When a saved search comes due, from the local time a person picked."""
from datetime import datetime
from types import SimpleNamespace

from app.services.saved_searches import next_due, schedule_label, schedule_problems


def setup(**overrides):
    return SimpleNamespace(**{"schedule_kind": "daily", "every_hours": 24, "time_of_day": "07:00",
                              "weekdays": [], "timezone": "UTC", "enabled": True, **overrides})


def test_a_search_that_never_comes_due_on_its_own():
    assert next_due(setup(schedule_kind="manual"), datetime(2026, 9, 20, 3, 0)) is None
    assert next_due(setup(enabled=False), datetime(2026, 9, 20, 3, 0)) is None
    assert next_due(setup(schedule_kind="weekly", weekdays=[]), datetime(2026, 9, 20, 3, 0)) is None
    assert schedule_label(setup(schedule_kind="manual")) == "Manual only"
    assert schedule_label(setup(enabled=False)) == "Paused"


def test_every_n_hours_counts_from_now():
    assert next_due(setup(schedule_kind="interval", every_hours=6), datetime(2026, 9, 20, 3, 0)) == datetime(2026, 9, 20, 9, 0)
    # Out-of-range values are clamped rather than skipping the search entirely.
    assert next_due(setup(schedule_kind="interval", every_hours=0), datetime(2026, 9, 20, 3, 0)) == datetime(2026, 9, 21, 3, 0)
    assert schedule_label(setup(schedule_kind="interval", every_hours=1)) == "Hourly"


def test_daily_runs_at_the_local_time_and_rolls_over_midnight():
    # 07:00 in Kolkata is 01:30 UTC; at 03:00 UTC today's slot has passed, so the next one is tomorrow's.
    assert next_due(setup(timezone="Asia/Kolkata"), datetime(2026, 9, 20, 3, 0)) == datetime(2026, 9, 21, 1, 30)
    assert next_due(setup(timezone="Asia/Kolkata"), datetime(2026, 9, 20, 0, 0)) == datetime(2026, 9, 20, 1, 30)
    assert schedule_label(setup(timezone="Asia/Kolkata")) == "Daily at 07:00 Asia/Kolkata"


def test_daily_keeps_the_local_hour_across_a_daylight_saving_change():
    # New York moves off daylight saving on 1 November 2026: 07:00 local goes from 11:00 UTC to 12:00 UTC.
    before = next_due(setup(timezone="America/New_York"), datetime(2026, 10, 31, 12, 0))
    after = next_due(setup(timezone="America/New_York"), datetime(2026, 11, 1, 13, 0))
    assert before == datetime(2026, 11, 1, 12, 0) and after == datetime(2026, 11, 2, 12, 0)


def test_weekly_finds_the_next_chosen_day():
    friday = setup(schedule_kind="weekly", weekdays=[4], time_of_day="09:30")
    assert next_due(friday, datetime(2026, 9, 20, 3, 0)) == datetime(2026, 9, 25, 9, 30)  # Sunday → Friday
    assert next_due(friday, datetime(2026, 9, 25, 10, 0)) == datetime(2026, 10, 2, 9, 30)  # past today's slot → next week
    weekdays = setup(schedule_kind="weekly", weekdays=[0, 2, 4], time_of_day="09:30")
    assert next_due(weekdays, datetime(2026, 9, 21, 10, 0)) == datetime(2026, 9, 23, 9, 30)
    assert schedule_label(weekdays) == "Mon, Wed, Fri at 09:30 UTC"


def test_an_unknown_time_zone_still_runs_the_search():
    """A zone the host has dropped falls back to UTC rather than leaving the search stranded."""
    assert next_due(setup(timezone="Mars/Olympus"), datetime(2026, 9, 20, 3, 0)) == datetime(2026, 9, 20, 7, 0)


def test_what_cannot_be_saved():
    assert schedule_problems(setup(schedule_kind="hourly")) == ["Unknown schedule 'hourly'"]
    assert schedule_problems(setup(time_of_day="7am")) == ["Time of day must look like 07:00"]
    assert schedule_problems(setup(timezone="Mars/Olympus")) == ["Unknown time zone 'Mars/Olympus'"]
    assert schedule_problems(setup(schedule_kind="weekly", weekdays=[])) == ["Pick at least one day of the week"]
    assert schedule_problems(setup(schedule_kind="interval", every_hours=500)) == ["Run every 1–168 hours"]
    assert schedule_problems(setup()) == [] and schedule_problems(setup(schedule_kind="manual", time_of_day="")) == []
