"""Bars, sessions and indicators built from an M1 BID+ASK price path.

* Bars sit on the FX calendar anchored at 17:00 New York: M15 and H1 as in the
  engine, D1 from 17:00 to 17:00 New York (five bars a week, no Sunday stub).
* A bar's OHLC uses only the minutes present in the path. ``minutes`` counts
  them; a bar is *complete* when no expected minute is missing.
* London local time follows UK summer time rules (last Sunday of March to last
  Sunday of October, 01:00 UTC), computed arithmetically like the NY calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import numpy as np
import numpy.typing as npt
from numpy.lib.stride_tricks import sliding_window_view

from fxnext.data.path import MinuteStatus, PricePath
from fxnext.domain.calendar import FxCalendar, ny_1700_utc
from fxnext.domain.timeutil import from_ns, to_ns

MIN_NS = 60_000_000_000
HOUR_NS = 60 * MIN_NS
DAY_NS = 24 * HOUR_NS
TF_NS = {"M15": 15 * MIN_NS, "H1": HOUR_NS, "D1": DAY_NS}
Tf = Literal["M15", "H1", "D1"]
Basis = Literal["BID", "MID"]
F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]

# Sessions in London local time (pre-registration S0/S7): name -> (start hour, end hour)
SESSIONS: dict[str, tuple[int, int]] = {"asia": (0, 7), "london": (7, 12), "ny": (12, 21)}


def _last_sunday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def uk_offset_hours(day: date) -> int:
    """UTC offset of London on ``day`` (1 in summer time, 0 otherwise), at midday."""

    return 1 if _last_sunday(day.year, 3) <= day < _last_sunday(day.year, 10) else 0


def london_window(day: date, start_hour: int, end_hour: int) -> tuple[int, int]:
    """UTC ns bounds of [start_hour, end_hour) London local time on a weekday."""

    off = timedelta(hours=uk_offset_hours(day))
    base = datetime(day.year, day.month, day.day, tzinfo=UTC) - off
    return to_ns(base + timedelta(hours=start_hour)), to_ns(base + timedelta(hours=end_hour))


def london_weekdays(start_ns: int, end_ns: int) -> list[date]:
    d = from_ns(start_ns).date() - timedelta(days=1)
    last = from_ns(end_ns).date() + timedelta(days=1)
    out = []
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def rollover_start_ns(instant_ns: int) -> int:
    """17:00 New York of the trading day containing ``instant_ns`` (start of rollover)."""

    d = from_ns(instant_ns).date()
    for cand in (d + timedelta(days=1), d, d - timedelta(days=1)):
        t = to_ns(ny_1700_utc(cand))
        if t <= instant_ns:
            return t
    raise AssertionError("unreachable")


def bar_grid(tf: Tf, start_ns: int, end_ns: int, calendar: FxCalendar | None = None) -> I64:
    """Open times of every expected bar with start <= open and open + step <= end."""

    calendar = calendar or FxCalendar()
    step = TF_NS[tf]
    chunks = []
    for s in calendar.weekly_sessions(from_ns(start_ns), from_ns(end_ns)):
        opens = np.arange(s.open_ns, s.close_ns - step + 1, step, dtype=np.int64)
        chunks.append(opens[(opens >= start_ns) & (opens + step <= end_ns)])
    return np.concatenate(chunks) if chunks else np.zeros(0, np.int64)


@dataclass(frozen=True, slots=True)
class Bars:
    symbol: str
    tf: Tf
    basis: Basis
    step_ns: int
    open_ns: I64
    minutes: I64  # minutes present in the path
    ticked: I64  # minutes with at least one tick (MinuteStatus.VALID)
    o: F64
    h: F64
    lo: F64
    c: F64

    def __len__(self) -> int:
        return int(self.open_ns.size)

    @property
    def expected_minutes(self) -> int:
        return self.step_ns // MIN_NS

    @property
    def close_ns(self) -> I64:
        return self.open_ns + self.step_ns

    @property
    def available(self) -> npt.NDArray[np.bool_]:
        return self.minutes > 0

    @property
    def complete(self) -> npt.NDArray[np.bool_]:
        return np.asarray(self.minutes == self.expected_minutes, dtype=np.bool_)

    @property
    def contiguous(self) -> npt.NDArray[np.bool_]:
        """True when bar i opens exactly at the close of bar i-1 (no weekend between)."""

        out = np.zeros(len(self), np.bool_)
        out[1:] = self.open_ns[1:] == self.open_ns[:-1] + self.step_ns
        return out


def price_columns(path: PricePath, basis: Basis) -> tuple[F64, F64, F64, F64]:
    cols = path.columns
    if basis == "BID":
        return cols["bid_open"], cols["bid_high"], cols["bid_low"], cols["bid_close"]
    return (
        (cols["bid_open"] + cols["ask_open"]) / 2.0,
        (cols["bid_high"] + cols["ask_high"]) / 2.0,
        (cols["bid_low"] + cols["ask_low"]) / 2.0,
        (cols["bid_close"] + cols["ask_close"]) / 2.0,
    )


def build_bars(path: PricePath, tf: Tf, basis: Basis) -> Bars:
    step = TF_NS[tf]
    grid = bar_grid(tf, path.coverage_start_ns, path.coverage_end_ns)
    n = grid.size
    t = path.columns["t_ns"]
    op, hi, lo, cl = price_columns(path, basis)
    idx = np.searchsorted(grid, t, side="right") - 1
    keep = (idx >= 0) & (t < grid[np.maximum(idx, 0)] + step) if n else np.zeros(t.size, bool)
    idx = idx[keep]
    op, hi, lo, cl = op[keep], hi[keep], lo[keep], cl[keep]
    ticked_min = (path.columns["status"][keep] == int(MinuteStatus.VALID)).astype(np.int64)
    o, h, low, c = (np.full(n, np.nan) for _ in range(4))
    minutes = np.zeros(n, np.int64)
    ticked = np.zeros(n, np.int64)
    if idx.size:
        starts = np.flatnonzero(np.r_[True, idx[1:] != idx[:-1]])
        ends = np.r_[starts[1:], idx.size]
        b = idx[starts]
        o[b] = op[starts]
        h[b] = np.maximum.reduceat(hi, starts)
        low[b] = np.minimum.reduceat(lo, starts)
        c[b] = cl[ends - 1]
        minutes[b] = ends - starts
        ticked[b] = np.add.reduceat(ticked_min, starts)
    for arr in (grid, minutes, ticked, o, h, low, c):
        arr.setflags(write=False)
    return Bars(path.symbol, tf, basis, step, grid, minutes, ticked, o, h, low, c)


def atr(bars: Bars, n: int) -> F64:
    """Simple-average ATR(n) at each bar, using bars i-n+1..i and the close of i-n.

    NaN when any of those bars (or the previous close) is missing. Across a
    weekend the previous close is Friday's, as on any trading platform.
    """

    out = np.full(len(bars), np.nan)
    if len(bars) <= n:
        return out
    prev_c = np.r_[np.nan, bars.c[:-1]]
    tr = np.maximum.reduce([bars.h - bars.lo, np.abs(bars.h - prev_c), np.abs(bars.lo - prev_c)])
    win = sliding_window_view(tr, n)  # win[k] = tr[k : k + n]
    out[n - 1 :] = win.mean(axis=1)  # NaN propagates
    return out


def prior_max(x: F64, n: int) -> F64:
    """max(x[i-n : i]) at each i (NaN if any is NaN or not enough history)."""

    out = np.full(x.size, np.nan)
    if x.size > n:
        out[n:] = sliding_window_view(x, n)[:-1].max(axis=1)
    return out


def prior_min(x: F64, n: int) -> F64:
    out = np.full(x.size, np.nan)
    if x.size > n:
        out[n:] = sliding_window_view(x, n)[:-1].min(axis=1)
    return out


def minute_index(path: PricePath, start_ns: int, end_ns: int) -> tuple[int, int]:
    t = path.columns["t_ns"]
    return int(np.searchsorted(t, start_ns)), int(np.searchsorted(t, end_ns))
