"""Tokyo business days and gotobi days (frozen text §3)."""

from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta

import jpholiday

GOTOBI_DAYS = (5, 10, 15, 20, 25)


def tokyo_business_day(d: date) -> bool:
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    if d.month == 1 and d.day <= 3:
        return False
    return not (d.month == 12 and d.day == 31)


def _previous_business_day(d: date) -> date:
    while not tokyo_business_day(d):
        d -= timedelta(days=1)
    return d


def gotobi_days(start: date, end: date) -> list[date]:
    """Gotobi days moved to the previous Tokyo business day, in [start, end)."""

    out: set[date] = set()
    y, m = start.year, start.month
    while date(y, m, 1) < end:
        last = _cal.monthrange(y, m)[1]
        for day in (*GOTOBI_DAYS, last):
            out.add(_previous_business_day(date(y, m, day)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(d for d in out if start <= d < end)


def control_days(start: date, end: date) -> list[date]:
    gotobi = set(gotobi_days(start, end))
    out, d = [], start
    while d < end:
        if tokyo_business_day(d) and d not in gotobi:
            out.append(d)
        d += timedelta(days=1)
    return out
