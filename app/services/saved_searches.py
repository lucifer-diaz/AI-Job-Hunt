"""When a saved Discovery search runs next.

Times are stored the way a person set them — "07:00, weekdays, Asia/Kolkata" — and turned into a
naive-UTC `next_run_at` the scheduler can compare against. Everything here is pure: the database
work lives with the endpoints in app.workspace.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEDULE_KINDS = ("manual", "interval", "daily", "weekly")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MIN_HOURS, MAX_HOURS = 1, 168
# A weekly search with an empty day list would never come due; look no further than this.
SEARCH_DAYS = 15


class Schedule(Protocol):
    """The scheduling fields of a SavedSearch — a plain object works just as well in tests."""

    schedule_kind: str
    every_hours: int
    time_of_day: str
    weekdays: list[int]
    timezone: str
    enabled: bool


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")  # a zone the host has dropped must not stop the search running


def _to_utc(local: datetime) -> datetime:
    return local.astimezone(UTC).replace(tzinfo=None)


def next_due(schedule: Schedule, after: datetime) -> datetime | None:
    """The first naive-UTC moment strictly after `after` when this search should start.

    None means it never comes due on its own: it is disabled, manual, or weekly with no days.
    """
    if not schedule.enabled or schedule.schedule_kind == "manual":
        return None
    if schedule.schedule_kind == "interval":
        hours = min(MAX_HOURS, max(MIN_HOURS, schedule.every_hours or 24))
        return after + timedelta(hours=hours)

    match = TIME_PATTERN.match(schedule.time_of_day or "")
    hour, minute = (int(match.group(1)), int(match.group(2))) if match else (7, 0)
    days = sorted({d for d in (schedule.weekdays or []) if 0 <= d <= 6}) if schedule.schedule_kind == "weekly" else None
    if days == []:
        return None

    zone = _zone(schedule.timezone)
    local_now = after.replace(tzinfo=UTC).astimezone(zone)
    for offset in range(SEARCH_DAYS):
        day = (local_now + timedelta(days=offset)).date()
        if days is not None and day.weekday() not in days:
            continue
        candidate = _to_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone))
        if candidate > after:
            return candidate
    return None


def schedule_label(schedule: Schedule) -> str:
    """One line describing the schedule, for the setup chips and the runs list."""
    if schedule.schedule_kind == "manual":
        return "Manual only"
    if not schedule.enabled:
        return "Paused"
    if schedule.schedule_kind == "interval":
        hours = schedule.every_hours or 24
        return "Hourly" if hours == 1 else f"Every {hours} hours"
    at = f"{schedule.time_of_day} {schedule.timezone}"
    if schedule.schedule_kind == "daily":
        return f"Daily at {at}"
    days = sorted({d for d in (schedule.weekdays or []) if 0 <= d <= 6})
    if not days:
        return "Weekly — no days picked"
    return f"{', '.join(DAY_NAMES[d] for d in days)} at {at}"


def schedule_problems(payload: Any) -> list[str]:
    """Why these schedule fields cannot be saved — empty when they are fine."""
    problems = []
    if payload.schedule_kind not in SCHEDULE_KINDS:
        return [f"Unknown schedule '{payload.schedule_kind}'"]
    if payload.schedule_kind == "interval" and not MIN_HOURS <= payload.every_hours <= MAX_HOURS:
        problems.append(f"Run every {MIN_HOURS}–{MAX_HOURS} hours")
    if payload.schedule_kind in ("daily", "weekly"):
        if not TIME_PATTERN.match(payload.time_of_day or ""):
            problems.append("Time of day must look like 07:00")
        if not valid_timezone(payload.timezone):
            problems.append(f"Unknown time zone '{payload.timezone}'")
    if payload.schedule_kind == "weekly":
        days = [d for d in (payload.weekdays or []) if 0 <= d <= 6]
        if not days:
            problems.append("Pick at least one day of the week")
    return problems
