"""Descriptive phases: S0 variance ratio, S7 phase 1, S3 phase 1 (no trades).

Exclusions shared by S0 and S7 (pre-registration S0):

* a bar (or a session) with a missing minute is excluded ("barre con buchi");
* a return whose two closes are not consecutive on the calendar is excluded,
  which removes every return that crosses a weekend or a holiday closure;
* intraday bars that overlap the rollover hour, 17:00-18:00 New York, are
  excluded; in the all-hours row the q-period sums skip that hour instead of
  stopping at it, otherwise H1 q=24 could never be formed. Session rows where q
  does not fit inside the session (H1 q=24) are reported as not computable.
  D1 bars close at 17:00 New York, so their returns are close to close at the
  rollover instant and nothing more is removed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np

from fxnext.data.path import MinuteStatus, PricePath
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.timeutil import from_ns
from fxnext.research.execution import PIP_SIZE
from fxnext.research.hyp.bars import (
    DAY_NS,
    F64,
    HOUR_NS,
    I64,
    MIN_NS,
    SESSIONS,
    Bars,
    Tf,
    atr,
    bar_grid,
    build_bars,
    london_weekdays,
    london_window,
    minute_index,
    price_columns,
)

Z95 = 1.959963984540054
Z_FLAG = 2.58
VR_SIZE = 0.05
VR_HORIZONS: dict[Tf, tuple[int, ...]] = {"M15": (2, 4, 16), "H1": (4, 24), "D1": (5, 20)}


# ----------------------------------------------------------------------------- S0


def variance_ratio(r: F64, seg: I64, q: int) -> dict[str, Any]:
    """Lo-MacKinlay VR(q) with the heteroskedasticity-robust z (1988, z*).

    Uses VR(q) = 1 + 2 sum_{j<q} (1 - j/q) rho(j) where rho(j) only pairs returns
    of the same segment (``seg``), so sums never bridge an excluded return.
    """

    n = int(r.size)
    out: dict[str, Any] = {"q": q, "returns": n, "segments": int(np.unique(seg).size)}
    if n < 2 * q:
        return out | {"status": "non calcolabile", "reason": "troppi pochi rendimenti"}
    e = r - r.mean()
    s2 = float((e * e).sum())
    if s2 <= 0.0:
        return out | {"status": "non calcolabile", "reason": "varianza nulla"}
    vr, theta = 1.0, 0.0
    for j in range(1, q):
        same = seg[j:] == seg[:-j]
        if not same.any():
            return out | {"status": "non calcolabile", "reason": f"nessuna coppia al lag {j}"}
        rho = float((e[j:] * e[:-j])[same].sum()) / s2
        delta = n * float((e[j:] ** 2 * e[:-j] ** 2)[same].sum()) / s2**2
        w = 2.0 * (1.0 - j / q)
        vr += w * rho
        theta += w * w * delta
    se = math.sqrt(theta / n)  # theta is the asymptotic variance of sqrt(n) (VR - 1)
    z = (vr - 1.0) / se if se > 0 else float("nan")
    if abs(z) > Z_FLAG and abs(vr - 1.0) >= VR_SIZE:
        label = "persistenza" if vr > 1.0 else "ritorno"
    else:
        label = "nessun segnale"
    return out | {
        "status": "ok",
        "vr": vr,
        "z": z,
        "ci_low": vr - Z95 * se,
        "ci_high": vr + Z95 * se,
        "label": label,
    }


def _rollover_mask(bars: Bars) -> np.ndarray:
    if bars.tf == "D1" or not len(bars):
        return np.zeros(len(bars), bool)
    d1 = bar_grid("D1", int(bars.open_ns[0]) - 2 * DAY_NS, int(bars.close_ns[-1]) + 2 * DAY_NS)
    pos = np.searchsorted(d1, bars.open_ns, side="right") - 1
    since = bars.open_ns - d1[np.maximum(pos, 0)]
    return (pos >= 0) & (since < HOUR_NS)


def return_table(bars: Bars, start_ns: int, end_ns: int) -> dict[str, Any]:
    """Valid mid log returns and their exclusion counts, inside [start, end)."""

    inside = (bars.open_ns >= start_ns) & (bars.close_ns <= end_ns)
    complete = bars.complete
    prev_complete = np.r_[False, complete[:-1]]
    contiguous = bars.contiguous
    roll = _rollover_mask(bars)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(bars.c / np.r_[np.nan, bars.c[:-1]])
    usable = inside & complete & prev_complete & contiguous
    ok = usable & ~roll
    counts = {
        "bars": int(inside.sum()),
        "excluded_incomplete": int((inside & ~(complete & prev_complete)).sum()),
        "excluded_not_consecutive": int((inside & complete & prev_complete & ~contiguous).sum()),
        "excluded_rollover": int((inside & complete & prev_complete & contiguous & roll).sum()),
        "returns": int(ok.sum()),
    }
    return {"r": r, "ok": ok, "usable": usable, "counts": counts}


def s0_symbol(path: PricePath, start_ns: int, end_ns: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tf, horizons in VR_HORIZONS.items():
        bars = build_bars(path, tf, "MID")
        table = return_table(bars, start_ns, end_ns)
        r, ok = table["r"], table["ok"]
        cells: dict[str, Any] = {"counts": table["counts"]}
        # all hours: runs of returns broken by missing bars and weekends, not by the
        # rollover hour, which is only removed (else H1 q=24 could never be formed)
        breaks = ~(table["usable"])
        seg_all = np.cumsum(breaks)
        rows = {"all": (r[ok], seg_all[ok])}
        if tf != "D1":
            for name, (h0, h1) in SESSIONS.items():
                seg = np.full(len(bars), -1, np.int64)
                for k, day in enumerate(london_weekdays(start_ns, end_ns)):
                    w0, w1 = london_window(day, h0, h1)
                    i0 = int(np.searchsorted(bars.open_ns, w0))
                    i1 = int(np.searchsorted(bars.close_ns, w1, side="right"))
                    seg[i0:i1] = k
                m = ok & (seg >= 0)
                # a run of valid returns inside the same session-day
                runs = np.cumsum(~m)
                rows[name] = (r[m], (seg * 1_000_000 + runs)[m])
        for name, (rr, ss) in rows.items():
            cells[name] = {f"q{q}": variance_ratio(rr, ss, q) for q in horizons}
        out[tf] = cells
    return out


# ----------------------------------------------------------------------- S7 phase 1

PREDICTED: dict[str, dict[str, int]] = {
    "EURUSD": {"london": -1, "ny": 1},
    "GBPUSD": {"london": -1, "ny": 1},
    "USDJPY": {"asia": 1, "ny": -1},
}
HOME = {"EURUSD": "london", "GBPUSD": "london", "USDJPY": "asia"}


@dataclass(frozen=True, slots=True)
class SessionReturn:
    symbol: str
    session: str
    day: date
    start_ns: int
    end_ns: int
    log_return: float


def session_returns(
    path: PricePath, start_ns: int, end_ns: int
) -> tuple[list[SessionReturn], dict[str, int]]:
    """Mid log return of each complete session-day (no missing minute)."""

    t = path.columns["t_ns"]
    mo, _, _, mc = price_columns(path, "MID")
    out: list[SessionReturn] = []
    excluded: dict[str, int] = {}
    for day in london_weekdays(start_ns, end_ns):
        for name, (h0, h1) in SESSIONS.items():
            w0, w1 = london_window(day, h0, h1)
            if w0 < start_ns or w1 > end_ns:
                continue
            i0, i1 = minute_index(path, w0, w1)
            expected = (w1 - w0) // MIN_NS
            if i1 - i0 != expected:
                key = "no_data" if i1 == i0 else "incomplete"
                excluded[f"{name}_{key}"] = excluded.get(f"{name}_{key}", 0) + 1
                continue
            assert int(t[i0]) == w0 and int(t[i1 - 1]) == w1 - MIN_NS
            ret = math.log(float(mc[i1 - 1]) / float(mo[i0]))
            out.append(SessionReturn(path.symbol, name, day, w0, w1, ret))
    return out, excluded


def _mean_ci(x: F64) -> dict[str, Any]:
    n = int(x.size)
    if n < 2:
        return {"n": n}
    mean, sd = float(x.mean()), float(x.std(ddof=1))
    se = sd / math.sqrt(n)
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "ci_low": mean - Z95 * se,
        "ci_high": mean + Z95 * se,
        "t": mean / se if se > 0 else float("nan"),
    }


def s7_phase1(rets: list[SessionReturn]) -> dict[str, Any]:
    """Per pair and session mean, legs with predicted sign, and the phase-2 gate."""

    per: dict[str, Any] = {}
    for sym in sorted({r.symbol for r in rets}):
        per[sym] = {}
        for name in SESSIONS:
            x = np.array([r.log_return for r in rets if r.symbol == sym and r.session == name])
            cell = _mean_ci(x)
            pred = PREDICTED.get(sym, {}).get(name)
            if pred is not None:
                cell["predicted_sign"] = pred
                cell["leg_passes"] = bool(
                    cell.get("mean") is not None
                    and np.sign(cell["mean"]) == pred
                    and (cell["ci_low"] > 0 if pred > 0 else cell["ci_high"] < 0)
                )
            per[sym][name] = cell
    legs: dict[str, Any] = {}
    for leg in ("home", "ny"):
        passing = [
            s
            for s in per
            if s in HOME and per[s][HOME[s] if leg == "home" else "ny"].get("leg_passes")
        ]
        legs[leg] = {"pairs_passing": passing, "passes": len(passing) >= 2}
        legs[leg]["pooled_by_day"] = _pooled_by_day(rets, leg)
    return {
        "per_symbol": per,
        "legs": legs,
        "phase2_variants": [
            v
            for v, need in (("A", ("home",)), ("B", ("ny",)), ("C", ("home", "ny")))
            if all(legs[x]["passes"] for x in need)
        ],
    }


def _pooled_by_day(rets: list[SessionReturn], leg: str) -> dict[str, Any]:
    """Mean of the sign-aligned returns over pairs, with a CI clustered by day."""

    xs, days = [], []
    for r in rets:
        if r.symbol not in HOME:
            continue
        name = HOME[r.symbol] if leg == "home" else "ny"
        if r.session != name:
            continue
        xs.append(PREDICTED[r.symbol][name] * r.log_return)
        days.append(r.day.toordinal())
    x = np.array(xs)
    if x.size < 2:
        return {"n": int(x.size)}
    mean = float(x.mean())
    _, inv = np.unique(np.array(days), return_inverse=True)
    sums = np.bincount(inv, weights=x - mean)
    g = sums.size
    se = math.sqrt(float((sums**2).sum()) * g / (g - 1)) / x.size
    return {
        "n": int(x.size),
        "days": int(g),
        "mean_aligned": mean,
        "ci_low": mean - Z95 * se,
        "ci_high": mean + Z95 * se,
    }


# ----------------------------------------------------------------------- S3 phase 1

_REOPEN_SEARCH_NS = 30 * MIN_NS


@dataclass(frozen=True, slots=True)
class Weekend:
    symbol: str
    friday_close_ns: int
    reopen_ns: int
    status: str  # "ok" or the exclusion reason
    c: float = math.nan  # last Friday mid
    o: float = math.nan  # first reopening mid
    first_tick_ns: int = 0
    first_spread_pips: float = math.nan
    median_spread_10m_pips: float = math.nan
    atr20: float = math.nan
    sigma20: float = math.nan
    closed_48h: bool = False
    gap_data_minutes_48h: int = 0

    @property
    def gap(self) -> float:
        return self.o - self.c


def weekends(path: PricePath, start_ns: int, end_ns: int) -> list[Weekend]:
    """Every Friday close -> Sunday reopening inside [start, end), with exclusions."""

    cal = FxCalendar()
    sessions = list(cal.weekly_sessions(from_ns(start_ns), from_ns(end_ns)))
    d1 = build_bars(path, "D1", "MID")
    atr20 = atr(d1, 20)
    t = path.columns["t_ns"]
    status = path.columns["status"]
    mo, mh, ml, mc = price_columns(path, "MID")
    spread = path.columns["ask_open"] - path.columns["bid_open"]
    pip = PIP_SIZE[path.symbol]
    out: list[Weekend] = []
    for prev, cur in zip(sessions, sessions[1:], strict=False):
        fri, sun = prev.close_ns, cur.open_ns
        if not (start_ns <= fri and sun + 2 * DAY_NS <= end_ns):
            continue
        if from_ns(fri).weekday() != 4 or from_ns(sun).weekday() != 6:
            out.append(Weekend(path.symbol, fri, sun, "not_a_weekend"))
            continue
        a, b = minute_index(path, fri - _REOPEN_SEARCH_NS, fri)
        fri_ok = np.flatnonzero(status[a:b] == int(MinuteStatus.VALID))
        if fri_ok.size == 0:
            out.append(Weekend(path.symbol, fri, sun, "no_friday_quote"))
            continue
        c = float(mc[a + int(fri_ok[-1])])
        a2, b2 = minute_index(path, sun, sun + _REOPEN_SEARCH_NS)
        sun_ok = np.flatnonzero(status[a2:b2] == int(MinuteStatus.VALID))
        if sun_ok.size == 0:
            out.append(Weekend(path.symbol, fri, sun, "no_reopen_quote"))
            continue
        j = a2 + int(sun_ok[0])
        k = int(np.searchsorted(d1.close_ns, fri))
        if k >= len(d1) or int(d1.close_ns[k]) != fri or math.isnan(atr20[k]) or k < 20:
            out.append(Weekend(path.symbol, fri, sun, "no_daily_history"))
            continue
        closes = d1.c[k - 20 : k + 1]
        if np.isnan(closes).any():
            out.append(Weekend(path.symbol, fri, sun, "no_daily_history"))
            continue
        sigma = float(np.diff(closes).std(ddof=1))
        o = float(mo[j])
        e0, e1 = minute_index(path, sun, sun + 2 * DAY_NS)
        expected = (2 * DAY_NS) // MIN_NS
        closed = bool((ml[j:e1] <= c).any()) if o >= c else bool((mh[j:e1] >= c).any())
        s10 = spread[j : min(j + 10, e1)] / pip
        out.append(
            Weekend(
                path.symbol,
                fri,
                sun,
                "ok",
                c=c,
                o=o,
                first_tick_ns=int(t[j]),
                first_spread_pips=float(spread[j] / pip),
                median_spread_10m_pips=float(np.median(s10)),
                atr20=float(atr20[k]),
                sigma20=sigma,
                closed_48h=closed,
                gap_data_minutes_48h=int(expected - (e1 - e0)),
            )
        )
    return out


def rw_closure_probability(gap: float, sigma: float) -> float:
    """2 * (1 - Phi(|G| / (sigma * sqrt 2))): random walk touches C within two days."""

    if sigma <= 0:
        return math.nan
    x = abs(gap) / (sigma * math.sqrt(2.0))
    phi = 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    return 2.0 * (1.0 - phi)


def s3_phase1(ws: list[Weekend], ks: tuple[float, ...]) -> dict[str, Any]:
    ok = [w for w in ws if w.status == "ok"]
    reasons: dict[str, int] = {}
    for w in ws:
        reasons[w.status] = reasons.get(w.status, 0) + 1
    data_check = {
        "weekends": len(ws),
        "by_status": dict(sorted(reasons.items())),
        "median_first_tick_delay_min": float(
            np.median([(w.first_tick_ns - w.reopen_ns) / MIN_NS for w in ok])
        )
        if ok
        else None,
        "median_first_spread_pips": float(np.median([w.first_spread_pips for w in ok]))
        if ok
        else None,
        "median_spread_10m_pips": float(np.median([w.median_spread_10m_pips for w in ok]))
        if ok
        else None,
        "weekends_with_missing_minutes_48h": sum(w.gap_data_minutes_48h > 0 for w in ok),
    }
    by_k: dict[str, Any] = {}
    for k in ks:
        sel = [w for w in ok if abs(w.gap) >= k * w.atr20]
        by_k[f"k{k:g}"] = {"pooled": _closure(sel)} | {
            s: _closure([w for w in sel if w.symbol == s]) for s in sorted({w.symbol for w in ok})
        }
    return {
        "data_check": data_check,
        "by_k": by_k,
        "phase2_k": [k for k in ks if by_k[f"k{k:g}"]["pooled"].get("passes")],
    }


def _closure(sel: list[Weekend]) -> dict[str, Any]:
    n = len(sel)
    if n == 0:
        return {"weekends": 0, "passes": False}
    p = np.array([rw_closure_probability(w.gap, w.sigma20) for w in sel])
    obs = float(np.mean([w.closed_48h for w in sel]))
    exp = float(p.mean())
    sd = math.sqrt(float((p * (1 - p)).sum())) / n
    low = obs - exp - Z95 * sd
    return {
        "weekends": n,
        "observed_rate": obs,
        "expected_rw_rate": exp,
        "difference": obs - exp,
        "difference_ci_low": low,
        "difference_ci_high": obs - exp + Z95 * sd,
        "passes": bool(low > 0),
    }


def weekend_rows(ws: list[Weekend]) -> list[dict[str, Any]]:
    return [asdict(w) | {"gap": w.gap} for w in ws]
