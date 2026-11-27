"""Events of h1-cbdrift-001: timing, expected change, positions and returns.

Readings of the frozen text that the code has to make explicit are listed in
``docs/M11-h1-cbdrift.md`` (section "Letture del testo congelato").
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.research.cbdrift.prereg import (
    ADMISSION_COVERAGE,
    AVERAGE_DAYS,
    BANKS,
    CANDIDATES,
    CBDRIFT_SPLITS,
    ENTRY_LAG,
    EXIT_LAG,
    MARKUP_PCT_PER_YEAR,
    MAX_FILL_WEEKDAYS,
    MEDIAN_DAYS,
    ONE_MONTH,
    OVERNIGHT,
    PAIRS,
    RATE_WARMUP_START,
    RBA_FIRST_MEETING,
    SIGNAL_LAG,
    SLIPPAGE_PIPS,
    SNB_FLOOR,
    THRESHOLD_BP,
)

F64 = npt.NDArray[np.float64]


def weekdays(first: date, end: date) -> list[date]:
    """Mon-Fri dates in [first, end)."""

    out = []
    d = first
    while d < end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def pip_size(symbol: str) -> float:
    return 1e-2 if symbol.endswith("JPY") else 1e-4


@dataclass
class RateGrid:
    """Rates on the weekday grid, in percent; raw (as published) and forward-filled."""

    days: list[date]
    raw: dict[str, F64]
    filled: dict[str, F64]

    @classmethod
    def build(cls, series: dict[str, dict[date, float]], days: list[date]) -> RateGrid:
        row = {d: i for i, d in enumerate(days)}
        raw: dict[str, F64] = {}
        filled: dict[str, F64] = {}
        for key, values in series.items():
            x = np.full(len(days), np.nan)
            for d, v in values.items():
                i = row.get(d)
                if i is not None:
                    x[i] = v
            raw[key] = x
            filled[key] = forward_fill(x, MAX_FILL_WEEKDAYS)
        return cls(days, raw, filled)


def forward_fill(x: F64, limit: int) -> F64:
    out = x.copy()
    last = math.nan
    gap = 0
    for i, v in enumerate(x):
        if not math.isnan(v):
            last, gap = float(v), 0
        else:
            gap += 1
            if gap <= limit:
                out[i] = last
    return out


@dataclass
class FxPanel:
    """Daily closes on weekdays of [warm-up, split end); NaN where a candle is not valid."""

    days: list[date]
    bid: dict[str, F64]  # by currency
    ask: dict[str, F64]
    raw_hashes: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)

    def valid(self, ccy: str) -> npt.NDArray[np.bool_]:
        return ~np.isnan(self.bid[ccy])


@dataclass(frozen=True, slots=True)
class Meeting:
    bank: str
    local_date: date
    scheduled: bool
    decision: str  # hike / cut / hold (report only)


@dataclass
class Event:
    bank: str
    currency: str
    announcement: date  # local date
    t_day: date  # T of the frozen text (RBNZ: local date - 1)
    decision: str
    signal_day: date | None = None
    entry_day: date | None = None
    exit_day: date | None = None
    expected_bp: float | None = None
    direction: int = 0  # +1 hike expected, -1 cut expected, 0 no position
    excluded: str | None = None
    legs: list[str] = field(default_factory=list)
    long_fx: float = 0.0  # long the currency (Fed: the basket) against USD
    long_carry: float = 0.0
    position: int = 0  # +1 long the currency/basket, -1 short
    spread: float = 0.0  # cost, positive number
    slippage: float = 0.0
    markup: float = 0.0
    zero_spread_filled: int = 0

    @property
    def fx(self) -> float:
        return self.position * self.long_fx

    @property
    def carry(self) -> float:
        return self.position * self.long_carry

    def cost(self, multiplier: float = 1.0, markup: bool = True) -> float:
        return multiplier * (self.spread + self.slippage + (self.markup if markup else 0.0))

    def net(self, multiplier: float = 1.0, markup: bool = True) -> float:
        return self.fx + self.carry - self.cost(multiplier, markup)

    @property
    def gross(self) -> float:
        return self.fx + self.carry


def admission(grid: RateGrid, split: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Currencies whose 1-month and overnight series have >= 90% of weekdays in the split."""

    start, end = CBDRIFT_SPLITS[split]
    sel = np.array([start <= d < end for d in grid.days])
    admitted = []
    info: dict[str, dict[str, Any]] = {}
    for ccy in ("USD",) + CANDIDATES:
        one = ONE_MONTH[ccy][split]
        on = OVERNIGHT[ccy]
        cov: dict[str, float | None] = {}
        for role, key in (("one_month", one), ("overnight", on)):
            if key is None or key not in grid.raw:
                cov[role] = None
            else:
                cov[role] = float((~np.isnan(grid.raw[key][sel])).mean())
        ok = all(c is not None and c >= ADMISSION_COVERAGE for c in cov.values())
        info[ccy] = {"one_month": one, "overnight": on, "coverage": cov, "admitted": ok}
        if ok and ccy != "USD":
            admitted.append(ccy)
    if not info["USD"]["admitted"]:
        raise ValueError("USD rates fail the admission rule: no run is possible")
    return admitted, info


def event_t_day(bank: str, local: date) -> date:
    """Frozen text §3.1: T is the local date; for the RBNZ always the local date - 1."""

    return local - timedelta(days=1) if bank == "RBNZ" else local


def build_events(
    meetings: list[Meeting],
    split: str,
    fx: FxPanel,
    grid: RateGrid,
    admitted: list[str],
) -> tuple[list[Event], dict[str, Any]]:
    start, end = CBDRIFT_SPLITS[split]
    fx_row = {d: i for i, d in enumerate(fx.days)}
    rate_row = {d: i for i, d in enumerate(grid.days)}
    basket_valid = np.ones(len(fx.days), dtype=bool)
    for ccy in admitted:
        basket_valid &= fx.valid(ccy)
    events: list[Event] = []
    reasons: Counter[str] = Counter()
    spread_fill = _median_spreads(fx, admitted, start, end)
    for m in sorted(meetings, key=lambda m: (m.local_date, m.bank)):
        ccy = BANKS[m.bank]
        t_day = event_t_day(m.bank, m.local_date)
        ev = Event(m.bank, ccy, m.local_date, t_day, m.decision)
        if t_day < start or t_day > end + timedelta(days=7):
            continue  # far outside the split: not an event of this run
        ev.excluded = _static_exclusion(m, ccy, admitted)
        if ev.excluded is None:
            valid = basket_valid if ccy == "USD" else fx.valid(ccy)
            _place(ev, valid, fx, start, end)
        if ev.excluded is None:
            _signal(ev, grid, rate_row, split)
        if ev.excluded is None and ev.direction != 0:
            legs = admitted if ccy == "USD" else [ccy]
            _returns(ev, legs, fx, fx_row, grid, rate_row, split, spread_fill)
        if ev.excluded is not None:
            reasons[ev.excluded] += 1
        events.append(ev)
    return events, {"excluded": dict(reasons), "zero_spread_median_rel": spread_fill}


def _static_exclusion(m: Meeting, ccy: str, admitted: list[str]) -> str | None:
    if not m.scheduled:
        return "unscheduled"
    if m.bank == "RBA" and m.local_date < RBA_FIRST_MEETING:
        return "rba_before_2007_12"
    if m.bank == "SNB" and SNB_FLOOR[0] <= m.local_date <= SNB_FLOOR[1]:
        return "snb_floor"
    if ccy != "USD" and ccy not in admitted:
        return "currency_not_admitted"
    return None


def _place(ev: Event, valid: npt.NDArray[np.bool_], fx: FxPanel, start: date, end: date) -> None:
    """S, entry and exit as the 12th, 11th and 1st valid weekday before T."""

    # A weekday in [end, T) is not loaded: the exit could fall there, so the event
    # cannot be shown to end inside the split.
    if any(d.weekday() < 5 for d in _span(end, ev.t_day)):
        ev.excluded = "exit_after_split"
        return
    before = [i for i, d in enumerate(fx.days) if d < ev.t_day and valid[i]]
    if len(before) < SIGNAL_LAG:
        ev.excluded = "not_enough_days"
        return
    ev.signal_day = fx.days[before[-SIGNAL_LAG]]
    ev.entry_day = fx.days[before[-ENTRY_LAG]]
    ev.exit_day = fx.days[before[-EXIT_LAG]]
    if ev.entry_day < start:
        ev.excluded = "entry_before_split"
    elif ev.exit_day >= end:
        ev.excluded = "exit_after_split"


def _span(a: date, b: date) -> list[date]:
    out = []
    d = a
    while d < b:
        out.append(d)
        d += timedelta(days=1)
    return out


def expected_change_bp(
    one: F64, on: F64, raw_one: F64, raw_on: F64, s_row: int, days_to_announcement: int
) -> tuple[float | None, str | None]:
    """Frozen text §3.2, in basis points. Returns (delta, reason if not computable)."""

    if s_row - AVERAGE_DAYS + 1 < 0 or s_row - MEDIAN_DAYS < 0:
        return None, "rates_warmup"
    window = slice(s_row - AVERAGE_DAYS + 1, s_row + 1)
    a, b = one[window], on[window]
    if np.isnan(a).any() or np.isnan(b).any():
        return None, "rates_missing_at_signal"
    spread = float(a.mean() - b.mean())
    hist = (raw_one - raw_on)[s_row - MEDIAN_DAYS : s_row]
    hist = hist[~np.isnan(hist)]
    if hist.size < ADMISSION_COVERAGE * MEDIAN_DAYS:
        return None, "rates_missing_in_median_window"
    corrected = spread - float(np.median(hist))
    if days_to_announcement >= 30:
        return None, "signal_too_far"
    return corrected * 30.0 / (30.0 - days_to_announcement) * 100.0, None


def _signal(ev: Event, grid: RateGrid, rate_row: dict[date, int], split: str) -> None:
    assert ev.signal_day is not None
    s_row = rate_row.get(ev.signal_day)
    if s_row is None:
        ev.excluded = "rates_warmup"
        return
    one_key = ONE_MONTH[ev.currency][split]
    on_key = OVERNIGHT[ev.currency]
    assert one_key is not None
    delta, why = expected_change_bp(
        grid.filled[one_key],
        grid.filled[on_key],
        grid.raw[one_key],
        grid.raw[on_key],
        s_row,
        (ev.announcement - ev.signal_day).days,
    )
    if delta is None:
        ev.excluded = why
        return
    ev.expected_bp = delta
    if delta >= THRESHOLD_BP:
        ev.direction = 1
    elif delta <= -THRESHOLD_BP:
        ev.direction = -1


def _median_spreads(fx: FxPanel, currencies: list[str], start: date, end: date) -> dict[str, float]:
    """Median non-zero relative spread of each pair over the valid weekdays of the split."""

    sel = np.array([start <= d < end for d in fx.days])
    out = {}
    for ccy in currencies:
        b, a = fx.bid[ccy][sel], fx.ask[ccy][sel]
        ok = ~np.isnan(b) & (a > b)
        rel = (a[ok] - b[ok]) / ((a[ok] + b[ok]) / 2.0)
        out[ccy] = float(np.median(rel)) if rel.size else math.nan
    return out


def swap_nights(entry: date, exit_: date) -> int:
    """Rollovers from the entry close to the exit close; Wednesday counts three."""

    n = 0
    for d in _span(entry, exit_):
        if d.weekday() < 5:
            n += 3 if d.weekday() == 2 else 1
    return n


def leg_return(
    ccy: str,
    fx: FxPanel,
    i0: int,
    i1: int,
    grid: RateGrid,
    r0: int,
    split: str,
    entry: date,
    exit_: date,
    spread_fill: dict[str, float],
) -> tuple[float, float, float, float, float, int]:
    """Long one unit of ``ccy`` against USD: fx, carry, spread, slippage, markup, filled."""

    symbol = PAIRS[ccy]
    b0, a0 = fx.bid[ccy][i0], fx.ask[ccy][i0]
    b1, a1 = fx.bid[ccy][i1], fx.ask[ccy][i1]
    m0, m1 = (b0 + a0) / 2.0, (b1 + a1) / 2.0
    usd_quoted = symbol.startswith("USD")
    fxr = (m0 / m1 - 1.0) if usd_quoted else (m1 / m0 - 1.0)
    filled = 0
    spread = 0.0
    for b, a, m in ((b0, a0, m0), (b1, a1, m1)):
        rel = (a - b) / m
        if rel <= 0:
            rel = spread_fill[ccy]
            filled += 1
        spread += rel / 2.0
    slip = 2.0 * SLIPPAGE_PIPS * pip_size(symbol) * (1.0 / m0 + 1.0 / m1) / 2.0
    one_ccy = ONE_MONTH[ccy][split]
    one_usd = ONE_MONTH["USD"][split]
    assert one_ccy is not None and one_usd is not None
    diff = grid.filled[one_ccy][r0] - grid.filled[one_usd][r0]
    carry = 0.0 if math.isnan(diff) else diff / 100.0 * (exit_ - entry).days / 360.0
    markup = MARKUP_PCT_PER_YEAR / 100.0 * swap_nights(entry, exit_) / 365.0
    return fxr, carry, spread, slip, markup, filled


def _returns(
    ev: Event,
    legs: list[str],
    fx: FxPanel,
    fx_row: dict[date, int],
    grid: RateGrid,
    rate_row: dict[date, int],
    split: str,
    spread_fill: dict[str, float],
) -> None:
    assert ev.entry_day is not None and ev.exit_day is not None
    i0, i1 = fx_row[ev.entry_day], fx_row[ev.exit_day]
    r0 = rate_row[ev.entry_day]
    # Long the currency on a hike; for the Fed, long USD = short the basket.
    ev.position = ev.direction if ev.currency != "USD" else -ev.direction
    w = 1.0 / len(legs)
    ev.legs = list(legs)
    for ccy in legs:
        fxr, carry, spread, slip, markup, filled = leg_return(
            ccy, fx, i0, i1, grid, r0, split, ev.entry_day, ev.exit_day, spread_fill
        )
        ev.long_fx += w * fxr
        ev.long_carry += w * carry
        ev.spread += w * spread
        ev.slippage += w * slip
        ev.markup += w * markup
        ev.zero_spread_filled += filled


def traded(events: list[Event]) -> list[Event]:
    return [e for e in events if e.excluded is None and e.direction != 0]


__all__ = [
    "Event",
    "FxPanel",
    "Meeting",
    "RateGrid",
    "admission",
    "build_events",
    "expected_change_bp",
    "event_t_day",
    "swap_nights",
    "traded",
    "weekdays",
    "RATE_WARMUP_START",
]
