"""Panel, signals, portfolio and statistics of l1-composito-001 (M10).

Rules: frozen text ``prereg-l1-composito-001-congelato-2026-11-26.md``. Months are
``(year, month)``; a signal is computed at the end of month t from what was
published by then, the position is held over month t+1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.research.composite.sources import (
    CURRENCIES,
    DEM_PER_EUR,
    DEM_SERIES,
    FX_SERIES,
    MACRO_ORDER,
    RATE_SERIES,
    SIGNS,
    VARIABLES,
    Series,
    macro_candidates,
)

Month = tuple[int, int]
F64 = npt.NDArray[np.float64]
NC = len(CURRENCIES)
WINDOWS = (12, 36, 60)
VALUE_WINDOW = 60
MONTHLY_LAG = 2  # months after the observation month (frozen text §3)
QUARTERLY_LAG = 5  # quarter start + 5 = end of the following quarter


def add_months(m: Month, n: int) -> Month:
    k = m[0] * 12 + (m[1] - 1) + n
    return (k // 12, k % 12 + 1)


def month_range(first: Month, last: Month) -> list[Month]:
    out, m = [], first
    while m <= last:
        out.append(m)
        m = add_months(m, 1)
    return out


def _month(d: date) -> Month:
    return (d.year, d.month)


def month_end_log_spot(
    series: Series, usd_per_unit: bool, factor: float = 1.0
) -> dict[Month, float]:
    """Log USD per unit of foreign currency at the last fixing of each month."""

    last: dict[Month, tuple[date, float]] = {}
    for d, v in series.obs.items():
        if v <= 0:
            continue
        m = _month(d)
        if m not in last or d > last[m][0]:
            last[m] = (d, v)
    out = {}
    for m, (_, v) in last.items():
        usd = v if usd_per_unit else factor / v
        out[m] = math.log(usd)
    return out


@dataclass
class Known:
    """Value of a macro series known at month ends, with its observation month."""

    obs: dict[Month, float]
    lag: int

    def latest(self, t: Month) -> Month | None:
        m = add_months(t, -self.lag)
        for _ in range(4):  # quarterly: the latest quarter start at or before m
            if m in self.obs:
                return m
            m = add_months(m, -1)
        return None

    def log_growth(self, t: Month, months: int) -> float | None:
        m = self.latest(t)
        if m is None:
            return None
        back = add_months(m, -months)
        a, b = self.obs.get(m), self.obs.get(back)
        if a is None or b is None or a <= 0 or b <= 0:
            return None
        return math.log(a) - math.log(b)

    def log_level(self, t: Month) -> float | None:
        m = self.latest(t)
        if m is None:
            return None
        v = self.obs[m]
        return math.log(v) if v > 0 else None


def known_from(series: Series) -> Known:
    obs = {_month(d): v for d, v in series.obs.items()}
    lag = QUARTERLY_LAG if series.frequency == "Q" else MONTHLY_LAG
    return Known(obs, lag)


@dataclass
class Panel:
    months: list[Month]
    spot: dict[str, dict[Month, float]]  # log USD per foreign unit, month end
    rates: dict[str, dict[Month, float]]  # percent per year
    macro: dict[str, dict[str, Known]]  # ccy -> variable -> known series
    chosen: dict[str, dict[str, str]]  # ccy -> variable -> FRED id
    quality: dict[str, Any] = field(default_factory=dict)


def choose_macro(available: dict[str, Series], ccy: str, variable: str) -> Series | None:
    """The candidate with the earliest observation; ties go to the first listed."""

    best: Series | None = None
    for sid in macro_candidates(ccy, variable):
        s = available.get(sid)
        if s is None or not s.obs:
            continue
        if best is None or min(s.obs) < min(best.obs):
            best = s
    return best


def build_panel(available: dict[str, Series], first: Month, last: Month) -> Panel:
    months = month_range(first, last)
    spot: dict[str, dict[Month, float]] = {}
    for ccy in CURRENCIES:
        sid, usd_per = FX_SERIES[ccy]
        s = available.get(sid)
        values = month_end_log_spot(s, usd_per) if s is not None else {}
        if ccy == "EUR":
            values = {m: v for m, v in values.items() if m >= (1999, 1)}
            dem = available.get(DEM_SERIES)
            if dem is not None:
                # USD per EUR-equivalent = (DEM per EUR) / (DEM per USD)
                old = month_end_log_spot(dem, False, DEM_PER_EUR)
                values.update({m: v for m, v in old.items() if m <= (1998, 12)})
        spot[ccy] = values
    rates: dict[str, dict[Month, float]] = {}
    for ccy in MACRO_ORDER:
        merged: dict[Month, float] = {}
        previous_end: Month | None = None
        for sid, until in RATE_SERIES[ccy]:
            s = available.get(sid)
            if s is None:
                continue
            for d, v in s.obs.items():
                m = _month(d)
                if previous_end is not None and m <= previous_end:
                    continue
                if until is not None and m > until:
                    continue
                merged[m] = v
            previous_end = until
        rates[ccy] = merged
    macro: dict[str, dict[str, Known]] = {}
    chosen: dict[str, dict[str, str]] = {}
    for ccy in MACRO_ORDER:
        macro[ccy], chosen[ccy] = {}, {}
        for var in VARIABLES:
            s = choose_macro(available, ccy, var)
            if s is not None:
                macro[ccy][var] = known_from(s)
                chosen[ccy][var] = f"{s.sid} ({s.frequency})"
    return Panel(months, spot, rates, macro, chosen)


def centered_ranks(values: dict[int, float]) -> dict[int, float]:
    """Ranks scaled to [-1, 1] (ties get the average rank)."""

    n = len(values)
    if n < 2:
        return {}
    order = sorted(values, key=lambda k: values[k])
    ranks: dict[int, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in order[i : j + 1]:
            ranks[k] = avg
        i = j + 1
    mid, half = (n + 1) / 2.0, (n - 1) / 2.0
    return {k: (r - mid) / half for k, r in ranks.items()}


def momentum_signal(panel: Panel, t: Month) -> dict[int, float]:
    """Signal A: mean over variables of the cross-sectional rank of the trend."""

    per_var: list[dict[int, float]] = []
    for var in VARIABLES:
        raw: dict[int, float] = {}
        for c, ccy in enumerate(CURRENCIES):
            k = panel.macro[ccy].get(var)
            if k is None:
                continue
            growths = [k.log_growth(t, w) for w in WINDOWS]
            if any(g is None for g in growths):
                continue
            raw[c] = SIGNS[var] * float(np.mean([g for g in growths if g is not None]))
        per_var.append(centered_ranks(raw))
    out: dict[int, float] = {}
    for c in range(NC):
        vals = [r[c] for r in per_var if c in r]
        if vals:
            out[c] = float(np.mean(vals))
    return out


def real_rate(panel: Panel, ccy: str, t: Month) -> float | None:
    s = panel.spot[ccy].get(t)
    pf = panel.macro[ccy].get("cpi")
    pu = panel.macro["USD"].get("cpi")
    if s is None or pf is None or pu is None:
        return None
    lf, lu = pf.log_level(t), pu.log_level(t)
    if lf is None or lu is None:
        return None
    return s + lf - lu


def value_signal(panel: Panel, t: Month) -> dict[int, float]:
    """Signal B: minus the 60-month change of the real exchange rate."""

    raw: dict[int, float] = {}
    back = add_months(t, -VALUE_WINDOW)
    for c, ccy in enumerate(CURRENCIES):
        q1, q0 = real_rate(panel, ccy, t), real_rate(panel, ccy, back)
        if q1 is not None and q0 is not None:
            raw[c] = -(q1 - q0)
    return centered_ranks(raw)


@dataclass
class Market:
    """One row per holding month (signal at the end of ``signal_months[j]``)."""

    signal_months: list[Month]
    composite: list[dict[int, float]]
    eligible: npt.NDArray[np.bool_]
    ret: F64  # excess log return of a long position in each currency (gross)
    half_spread: F64  # relative, per side
    eligible_counts: list[int]


def _carry(panel: Panel, ccy: str, t: Month, rate_lag: int) -> float | None:
    known = add_months(t, -rate_lag)
    a, b = panel.rates[ccy].get(known), panel.rates["USD"].get(known)
    if a is None or b is None:
        return None
    return (a - b) / 100.0 / 12.0


def build_market(
    panel: Panel,
    signal_months: list[Month],
    spread_pips: dict[str, float],
    rate_lag: int,
) -> Market:
    p = len(signal_months)
    eligible = np.zeros((p, NC), dtype=bool)
    ret = np.zeros((p, NC))
    half = np.zeros((p, NC))
    comps: list[dict[int, float]] = []
    counts: list[int] = []
    for j, t in enumerate(signal_months):
        a, b = momentum_signal(panel, t), value_signal(panel, t)
        nxt = add_months(t, 1)
        comp: dict[int, float] = {}
        for c, ccy in enumerate(CURRENCIES):
            s0, s1 = panel.spot[ccy].get(t), panel.spot[ccy].get(nxt)
            carry = _carry(panel, ccy, t, rate_lag)
            if c not in a or c not in b or s0 is None or s1 is None or carry is None:
                continue
            eligible[j, c] = True
            comp[c] = (a[c] + b[c]) / 2.0
            ret[j, c] = (s1 - s0) + carry
            pair_price = math.exp(s0) if FX_SERIES[ccy][1] else math.exp(-s0)
            pip = 0.01 if ccy == "JPY" else 0.0001
            half[j, c] = spread_pips[ccy] * pip / pair_price / 2.0
        comps.append(comp)
        counts.append(len(comp))
    return Market(signal_months, comps, eligible, ret, half, counts)


def weights_from(scores: dict[int, float], min_count: int) -> F64:
    """Dollar neutral, proportional to the centered rank: +1 long, -1 short."""

    w = np.zeros(NC)
    if len(scores) < min_count:
        return w
    ranks = centered_ranks(scores)
    pos = sum(r for r in ranks.values() if r > 0)
    neg = -sum(r for r in ranks.values() if r < 0)
    if pos <= 0 or neg <= 0:
        return w
    for c, r in ranks.items():
        w[c] = r / pos if r > 0 else r / neg
    return w


@dataclass
class Returns:
    gross: F64
    markup: F64
    spread: F64

    def net(self, multiplier: float = 1.0) -> F64:
        out: F64 = self.gross - multiplier * (self.markup + self.spread)
        return out


def simulate(market: Market, weights: F64, markup_pct: F64) -> Returns:
    p = weights.shape[0]
    gross = (weights * market.ret).sum(axis=1)
    markup = (np.abs(weights) * markup_pct[None, :] / 100.0 / 12.0).sum(axis=1)
    prev = np.vstack([np.zeros((1, NC)), weights[:-1]]) if p else weights
    spread = (np.abs(weights - prev) * market.half_spread).sum(axis=1)
    return Returns(gross, markup, spread)


def composite_weights(market: Market, min_count: int) -> F64:
    return (
        np.vstack([weights_from(c, min_count) for c in market.composite])
        if market.composite
        else np.zeros((0, NC))
    )


def random_weights(market: Market, min_count: int, rng: np.random.Generator) -> F64:
    rows = []
    for comp in market.composite:
        keys = list(comp)
        perm = rng.permutation(len(keys))
        rows.append(weights_from({k: float(perm[i]) for i, k in enumerate(keys)}, min_count))
    return np.vstack(rows) if rows else np.zeros((0, NC))


def newey_west_t(x: F64, lags: int) -> float | None:
    n = x.size
    if n < 3:
        return None
    mu = float(x.mean())
    e = x - mu
    s = float(e @ e) / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1.0 - k / (lags + 1.0)
        s += 2.0 * w * float(e[k:] @ e[:-k]) / n
    if s <= 0:
        return None
    return mu / math.sqrt(s / n)


def stats(x: F64, lags: int) -> dict[str, Any]:
    if x.size == 0:
        return {"months": 0}
    sd = float(x.std(ddof=1)) if x.size > 1 else float("nan")
    eq = np.cumsum(x)
    dd = float((np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq).max())
    return {
        "months": int(x.size),
        "mean_pct_month": float(x.mean()) * 100.0,
        "mean_pct_year": float(x.mean()) * 1200.0,
        "sd_pct_year": sd * math.sqrt(12.0) * 100.0,
        "sharpe_year": float(x.mean()) / sd * math.sqrt(12.0) if sd and sd > 0 else None,
        "t_newey_west": newey_west_t(x, lags),
        "max_drawdown_pct": dd * 100.0,
        "positive_months_pct": float((x > 0).mean()) * 100.0,
    }
