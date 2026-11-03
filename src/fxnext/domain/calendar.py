"""FX trading calendar anchored to the 17:00 America/New_York close (ADR-0009, ADR-0011).

US daylight-saving rules are implemented arithmetically (no tz database), so the
calendar is identical on every platform:

* 2007 onwards: second Sunday of March .. first Sunday of November;
* 1987-2006: first Sunday of April .. last Sunday of October.

A trading week runs from Sunday 17:00 to Friday 17:00 New York time. US DST
changes happen on Sunday at 02:00 local, before the weekly open, so a trading
week never contains a transition and lasts exactly 120 hours in UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
import numpy.typing as npt

from fxnext.domain.enums import Timeframe
from fxnext.domain.timeutil import from_ns, to_ns

CALENDAR_VERSION = "fx-calendar-v1"
_HOUR_NS = 3_600_000_000_000
_WEEK_SESSION_NS = 120 * _HOUR_NS


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (6 - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_sunday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def _dst_bounds(year: int) -> tuple[date, date]:
    if year >= 2007:
        return _nth_sunday(year, 3, 2), _nth_sunday(year, 11, 1)
    if year >= 1987:
        return _nth_sunday(year, 4, 1), _last_sunday(year, 10)
    raise ValueError("calendar supports years from 1987")


def ny_utc_offset_hours_at_1700(day: date) -> int:
    """UTC offset of New York at 17:00 local on ``day`` (-4 in DST, -5 otherwise)."""

    start, end = _dst_bounds(day.year)
    return -4 if start <= day < end else -5


def ny_1700_utc(day: date) -> datetime:
    offset = ny_utc_offset_hours_at_1700(day)
    return datetime(day.year, day.month, day.day, 17, tzinfo=UTC) - timedelta(hours=offset)


@dataclass(frozen=True, slots=True)
class Session:
    open_ns: int
    close_ns: int


# Expected closures: the NY trading day ending at 17:00 NY on these month/day dates.
_CLOSURE_DATES = ((12, 25), (1, 1))


class FxCalendar:
    """Pure, deterministic calendar of expected slots."""

    calendar_version = CALENDAR_VERSION

    def closures(self, year: int) -> tuple[Session, ...]:
        out = []
        for month, day in _CLOSURE_DATES:
            d = date(year, month, day)
            if d.weekday() < 5:  # Monday..Friday trading days only
                end = ny_1700_utc(d)
                start = ny_1700_utc(d - timedelta(days=1))
                out.append(Session(to_ns(start), to_ns(end)))
        return tuple(out)

    def weekly_sessions(self, start: datetime, end: datetime) -> Iterator[Session]:
        """Weekly sessions intersecting [start, end), closures removed."""

        day = start.date() - timedelta(days=7)
        day -= timedelta(days=(day.weekday() + 1) % 7)  # back to a Sunday
        end_ns = to_ns(end)
        start_ns = to_ns(start)
        while True:
            open_ns = to_ns(ny_1700_utc(day))
            if open_ns >= end_ns:
                return
            close_ns = open_ns + _WEEK_SESSION_NS
            if close_ns > start_ns:
                yield from self._split_by_closures(Session(open_ns, close_ns))
            day += timedelta(days=7)

    def _split_by_closures(self, session: Session) -> Iterator[Session]:
        years = {from_ns(session.open_ns).year, from_ns(session.close_ns).year}
        cuts = sorted(
            (
                c
                for y in years
                for c in self.closures(y)
                if c.open_ns < session.close_ns and c.close_ns > session.open_ns
            ),
            key=lambda c: c.open_ns,
        )
        cursor = session.open_ns
        for cut in cuts:
            if cut.open_ns > cursor:
                yield Session(cursor, cut.open_ns)
            cursor = max(cursor, cut.close_ns)
        if cursor < session.close_ns:
            yield Session(cursor, session.close_ns)

    def slot_grid(self, tf: Timeframe, start: datetime, end: datetime) -> npt.NDArray[np.int64]:
        """Open times (ns) of every expected slot with start <= open and close <= end."""

        start_ns, end_ns, step = to_ns(start), to_ns(end), tf.ns
        chunks = []
        for session in self.weekly_sessions(start, end):
            first = session.open_ns
            opens = np.arange(first, session.close_ns - step + 1, step, dtype=np.int64)
            mask = (opens >= start_ns) & (opens + step <= end_ns)
            chunks.append(opens[mask])
        if not chunks:
            return np.empty(0, dtype=np.int64)
        return np.concatenate(chunks)

    def is_in_session(self, instant: datetime) -> bool:
        ns = to_ns(instant)
        return any(
            s.open_ns <= ns < s.close_ns
            for s in self.weekly_sessions(instant - timedelta(days=8), instant + timedelta(days=1))
        )

    def slot_ending_at(self, tf: Timeframe, close_time: datetime) -> int | None:
        """Open time (ns) of the expected slot closing exactly at ``close_time``."""

        grid = self.slot_grid(tf, close_time - tf.duration, close_time)
        target = to_ns(close_time) - tf.ns
        return int(target) if grid.size and grid[-1] == target else None

    def window_ending_at(
        self, tf: Timeframe, last_open_ns: int, slots_back: int
    ) -> npt.NDArray[np.int64]:
        """``slots_back + 1`` consecutive expected slots ending with ``last_open_ns``."""

        span = timedelta(days=14)
        end = from_ns(last_open_ns + tf.ns)
        while True:
            grid = self.slot_grid(tf, end - span, end)
            idx = np.searchsorted(grid, last_open_ns)
            if idx >= grid.size or grid[idx] != last_open_ns:
                raise ValueError("last_open_ns is not an expected slot")
            if idx >= slots_back:
                return grid[idx - slots_back : idx + 1]
            span *= 2
            if span > timedelta(days=3650):
                raise ValueError("window exceeds calendar search range")

    def next_slot_open(self, tf: Timeframe, open_ns: int) -> int | None:
        """Open of the expected slot after ``open_ns`` if it starts at that slot's close."""

        close_ns = open_ns + tf.ns
        grid = self.slot_grid(tf, from_ns(close_ns), from_ns(close_ns + tf.ns))
        return int(grid[0]) if grid.size and grid[0] == close_ns else None
