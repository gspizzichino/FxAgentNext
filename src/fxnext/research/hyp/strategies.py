"""Trading rules of S1, S2, S3 and S7 (pre-registrations frozen 2026-11-11).

Each rule turns bars of the development split into ``TradeSpec`` objects that the
M2 simulator executes unchanged (fills on the right side, costs, position policy).
A rule only reads bars whose close is at or before the instant the trade becomes
available; ``tests/test_t14_hypotheses.py`` checks this by perturbing the future.

Benchmarks and the entry test need more than the candidates:

* ``mirror``: the LONG and SHORT trade at the same instant with the same R
  (random_direction);
* ``pool``: every eligible instant with the variant's geometry (random_time);
* ``neutral``: the same entry with a fixed N-bar exit, or a symmetric 1 R
  stop and target (entry test, common rule 8).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

import numpy as np

from fxnext.data.path import PricePath
from fxnext.domain.enums import Direction
from fxnext.domain.timeutil import from_ns
from fxnext.research.execution import TradeSpec
from fxnext.research.hyp.bars import (
    DAY_NS,
    F64,
    HOUR_NS,
    MIN_NS,
    SESSIONS,
    Bars,
    Tf,
    atr,
    build_bars,
    london_weekdays,
    london_window,
    minute_index,
    price_columns,
    prior_max,
    prior_min,
)
from fxnext.research.hyp.desc import HOME, PREDICTED, Weekend, weekends
from fxnext.research.hyp.prereg import Variant

_ENTRY_WINDOW_NS = 15 * MIN_NS  # S3 and S7: first valid minute within 15 minutes
NeutralKind = Literal["nbar", "sym1r"]


@dataclass
class Context:
    """One symbol over one split. ``path`` is already truncated at the split end."""

    path: PricePath
    start_ns: int
    end_ns: int
    _bars: dict[tuple[str, str], Bars] = field(default_factory=dict)
    _weekends: list[Weekend] | None = None

    @property
    def symbol(self) -> str:
        return self.path.symbol

    @property
    def sentinel_ns(self) -> int:
        """An exit after the end of the data: the simulator reports END_OF_DATA."""

        return self.path.coverage_end_ns + DAY_NS

    def bars(self, tf: Tf, basis: Literal["BID", "MID"]) -> Bars:
        key = (tf, basis)
        if key not in self._bars:
            self._bars[key] = build_bars(self.path, tf, basis)
        return self._bars[key]

    def weekends(self) -> list[Weekend]:
        if self._weekends is None:
            self._weekends = weekends(self.path, self.start_ns, self.end_ns)
        return self._weekends

    def in_split(self, ns: int) -> bool:
        return self.start_ns <= ns < self.end_ns


@dataclass(frozen=True, slots=True)
class Meta:
    """What the benchmark and the entry test need to rebuild a trade."""

    entry_bar: int = -1
    atr: float = math.nan
    extra: dict[str, Any] = field(default_factory=dict)


Candidates = list[tuple[TradeSpec, Meta]]
Pair = tuple[TradeSpec, TradeSpec]


class Rule(Protocol):
    random_direction: bool

    def candidates(self, ctx: Context, v: Variant) -> Candidates: ...

    def mirror(self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta) -> Pair | None: ...

    def pool(self, ctx: Context, v: Variant, ratio: float) -> list[Pair] | None: ...

    def neutral(
        self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta, kind: NeutralKind, n: int
    ) -> TradeSpec | None: ...


def _sign(d: Direction) -> float:
    return 1.0 if d is Direction.LONG else -1.0


def _key(ctx: Context, v: Variant, d: Direction, at_ns: int, tag: str = "") -> str:
    return f"{ctx.symbol}:{v.name}:{d.value}:{at_ns}{tag}"


def _spec(
    key: str,
    ctx: Context,
    d: Direction,
    entry: float,
    risk: float,
    target_r: float | None,
    available: int,
    expires: int,
    exit_at: int,
    target_price: float | None = None,
) -> TradeSpec:
    s = _sign(d)
    if target_price is not None:
        target = target_price
    elif target_r is None:
        target = s * math.inf
    else:
        target = entry + s * target_r * risk
    return TradeSpec(
        key, ctx.symbol, d, entry, entry - s * risk, target, available, expires, exit_at
    )


def _both(spec: TradeSpec, risk: float, target_r: float | None, key: str) -> Pair:
    out = []
    for d in (Direction.LONG, Direction.SHORT):
        s = _sign(d)
        target = s * math.inf if target_r is None else spec.entry_ref + s * target_r * risk
        out.append(
            replace(
                spec,
                key=f"{key}:{d.value}",
                direction=d,
                stop=spec.entry_ref - s * risk,
                target=target,
            )
        )
    return out[0], out[1]


def _neutral_common(
    spec: TradeSpec, kind: NeutralKind, nbar_exit_ns: int, sym_exit_ns: int | None
) -> TradeSpec:
    s, risk = _sign(spec.direction), spec.risk
    if kind == "nbar":
        return replace(
            spec,
            key=f"{spec.key}:nbar",
            stop=-s * math.inf,
            target=s * math.inf,
            exit_at_ns=nbar_exit_ns,
            unit_risk=risk,
        )
    return replace(
        spec,
        key=f"{spec.key}:sym1r",
        target=spec.entry_ref + s * risk,
        exit_at_ns=sym_exit_ns if sym_exit_ns is not None else spec.exit_at_ns,
    )


def _bar_open(bars: Bars, i: int, ctx: Context) -> int:
    return int(bars.open_ns[i]) if i < len(bars) else ctx.sentinel_ns


# ------------------------------------------------------------------------------ S1


class FailureTest:
    """S1 ``s1-failure-dev-001``: a 20-bar breakout that closes back inside, faded."""

    random_direction = True

    channel = 20
    atr_n = 14
    stop_buffer_atr = 0.1
    min_stop_atr = 0.5
    time_exit_bars = 120

    def _arrays(self, ctx: Context, tf: Tf) -> tuple[Bars, F64, F64, F64]:
        b = ctx.bars(tf, "BID")
        return b, atr(b, self.atr_n), prior_max(b.h, self.channel), prior_min(b.lo, self.channel)

    def candidates(self, ctx: Context, v: Variant) -> Candidates:
        assert v.tf is not None and v.w is not None and v.k is not None
        b, a14, upper, lower = self._arrays(ctx, v.tf)
        n = len(b)
        cont = b.contiguous
        used_levels: set[tuple[Direction, float, int]] = set()
        seen: set[tuple[Direction, int]] = set()
        out: Candidates = []
        for i in range(self.channel, n - 1):
            for d, level, broke in (
                (Direction.SHORT, upper[i], b.h[i] > upper[i]),
                (Direction.LONG, lower[i], b.lo[i] < lower[i]),
            ):
                if not broke or math.isnan(level):
                    continue
                f = self._failure(b, i, d, level, v.w)
                if f is None or f + 1 >= n or not cont[f + 1] or not b.available[f + 1]:
                    continue
                if math.isnan(a14[f]) or not ctx.in_split(int(b.close_ns[f])):
                    continue
                window = (
                    b.h[i - self.channel : i]
                    if d is Direction.SHORT
                    else b.lo[i - self.channel : i]
                )
                src = (
                    i
                    - self.channel
                    + int(np.argmax(window) if d is Direction.SHORT else np.argmin(window))
                )
                lvl_key = (d, float(level), src)
                if lvl_key in used_levels or (d, f) in seen:
                    continue
                used_levels.add(lvl_key)
                seen.add((d, f))
                entry = float(b.c[f])
                if d is Direction.SHORT:
                    extreme = float(b.h[i : f + 1].max())
                    stop = max(
                        extreme + self.stop_buffer_atr * a14[f], entry + self.min_stop_atr * a14[f]
                    )
                else:
                    extreme = float(b.lo[i : f + 1].min())
                    stop = min(
                        extreme - self.stop_buffer_atr * a14[f], entry - self.min_stop_atr * a14[f]
                    )
                risk = abs(stop - entry)
                avail = int(b.close_ns[f])
                spec = _spec(
                    _key(ctx, v, d, avail),
                    ctx,
                    d,
                    entry,
                    risk,
                    v.k,
                    avail,
                    int(b.close_ns[f + 1]),
                    _bar_open(b, f + 1 + self.time_exit_bars, ctx),
                )
                meta = Meta(
                    f + 1,
                    float(a14[f]),
                    {"level": float(level), "breakout_bar_open": int(b.open_ns[i])},
                )
                out.append((spec, meta))
        out.sort(key=lambda x: (x[0].available_at_ns, x[0].key))
        return out

    @staticmethod
    def _failure(b: Bars, i: int, d: Direction, level: float, w: int) -> int | None:
        for f in range(i, min(i + w + 1, len(b))):
            if f > i and (not b.contiguous[f] or not b.available[f]):
                return None
            if (d is Direction.SHORT and b.c[f] <= level) or (
                d is Direction.LONG and b.c[f] >= level
            ):
                return f
        return None

    def baseline_overlap(self, ctx: Context, v: Variant, cands: Candidates) -> dict[str, Any]:
        """Breakouts by the baseline rule (close beyond L, contracts §11) that became failure tests.

        The baseline sees only bars that close beyond the channel, so with w = 0 none
        of its breakouts can be a failure test; with w = 1 a baseline breakout becomes
        one when the next bar closes back inside.
        """

        assert v.tf is not None
        b, _, upper, lower = self._arrays(ctx, v.tf)
        upper_prev = np.r_[np.nan, upper[:-1]]
        lower_prev = np.r_[np.nan, lower[:-1]]
        prev_c = np.r_[np.nan, b.c[:-1]]
        inside = (b.close_ns > ctx.start_ns) & (b.close_ns <= ctx.end_ns)
        up = inside & (b.c > upper) & (prev_c <= upper_prev)
        down = inside & (b.c < lower) & (prev_c >= lower_prev)
        starts = {m.extra["breakout_bar_open"] for _, m in cands}
        opens = b.open_ns
        became = sum(int(opens[i]) in starts for i in np.flatnonzero(up | down).tolist())
        return {
            "timeframe": v.tf,
            "baseline_breakouts": int(up.sum() + down.sum()),
            "became_failure_test": int(became),
        }

    def mirror(self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta) -> Pair:
        return _both(spec, spec.risk, v.k, f"bench:{spec.key}")

    def pool(self, ctx: Context, v: Variant, ratio: float) -> list[Pair]:
        assert v.tf is not None
        b, a14, _, _ = self._arrays(ctx, v.tf)
        n, cont = len(b), b.contiguous
        out: list[Pair] = []
        for f in range(n - 1):
            if math.isnan(a14[f]) or not cont[f + 1] or not ctx.in_split(int(b.close_ns[f])):
                continue
            if math.isnan(b.c[f]):
                continue
            avail = int(b.close_ns[f])
            base = TradeSpec(
                f"pool:{ctx.symbol}:{v.name}:{avail}",
                ctx.symbol,
                Direction.LONG,
                float(b.c[f]),
                0.0,
                0.0,
                avail,
                int(b.close_ns[f + 1]),
                _bar_open(b, f + 1 + self.time_exit_bars, ctx),
            )
            out.append(_both(base, ratio * float(a14[f]), v.k, base.key))
        return out

    def neutral(
        self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta, kind: NeutralKind, n: int
    ) -> TradeSpec:
        assert v.tf is not None
        b = ctx.bars(v.tf, "BID")
        return _neutral_common(spec, kind, _bar_open(b, meta.entry_bar + n, ctx), None)


# ------------------------------------------------------------------------------ S2


class DailyTrend:
    """S2 ``s2-dtrend-dev-001``: N-day close breakout, exit on the M-day channel."""

    random_direction = True

    atr_n = 20
    time_exit_days = 250

    def _arrays(self, ctx: Context, v: Variant) -> tuple[Bars, F64, F64, F64, F64, F64]:
        assert v.n is not None and v.m is not None
        b = ctx.bars("D1", "BID")
        return (
            b,
            atr(b, self.atr_n),
            prior_max(b.h, v.n),
            prior_min(b.lo, v.n),
            prior_max(b.h, v.m),
            prior_min(b.lo, v.m),
        )

    def _exit_at(
        self, ctx: Context, b: Bars, hi_m: F64, lo_m: F64, e: int, d: Direction, channel: bool
    ) -> int:
        last = min(len(b), e + self.time_exit_days)
        if channel:
            for j in range(e, last):
                hit = b.c[j] < lo_m[j] if d is Direction.LONG else b.c[j] > hi_m[j]
                if hit:  # NaN compares False: a missing day never triggers
                    return int(b.open_ns[j + 1]) + MIN_NS if j + 1 < len(b) else ctx.sentinel_ns
        return _bar_open(b, e + self.time_exit_days, ctx)

    def candidates(self, ctx: Context, v: Variant) -> Candidates:
        assert v.s is not None
        b, a20, hi_n, lo_n, hi_m, lo_m = self._arrays(ctx, v)
        out: Candidates = []
        for i in range(len(b) - 1):
            if math.isnan(a20[i]) or not ctx.in_split(int(b.close_ns[i])):
                continue
            for d, hit in (
                (Direction.LONG, b.c[i] > hi_n[i]),
                (Direction.SHORT, b.c[i] < lo_n[i]),
            ):
                if not hit:
                    continue
                e = i + 1
                avail = int(b.open_ns[e])
                spec = _spec(
                    _key(ctx, v, d, avail),
                    ctx,
                    d,
                    float(b.c[i]),
                    v.s * float(a20[i]),
                    None,
                    avail,
                    int(b.close_ns[e]),
                    self._exit_at(ctx, b, hi_m, lo_m, e, d, channel=True),
                )
                out.append((spec, Meta(e, float(a20[i]))))
        out.sort(key=lambda x: (x[0].available_at_ns, x[0].key))
        return out

    def mirror(self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta) -> Pair:
        b, _, _, _, hi_m, lo_m = self._arrays(ctx, v)
        long, short = _both(spec, spec.risk, None, f"bench:{spec.key}")
        return (
            replace(
                long,
                exit_at_ns=self._exit_at(ctx, b, hi_m, lo_m, meta.entry_bar, Direction.LONG, True),
            ),
            replace(
                short,
                exit_at_ns=self._exit_at(ctx, b, hi_m, lo_m, meta.entry_bar, Direction.SHORT, True),
            ),
        )

    def pool(self, ctx: Context, v: Variant, ratio: float) -> list[Pair]:
        assert v.s is not None
        b, a20, _, _, hi_m, lo_m = self._arrays(ctx, v)
        out: list[Pair] = []
        for i in range(len(b) - 1):
            if math.isnan(a20[i]) or math.isnan(b.c[i]) or not ctx.in_split(int(b.close_ns[i])):
                continue
            e = i + 1
            avail = int(b.open_ns[e])
            base = TradeSpec(
                f"pool:{ctx.symbol}:{v.name}:{avail}",
                ctx.symbol,
                Direction.LONG,
                float(b.c[i]),
                0.0,
                0.0,
                avail,
                int(b.close_ns[e]),
                None,
            )
            long, short = _both(base, v.s * float(a20[i]), None, base.key)
            out.append(
                (
                    replace(
                        long, exit_at_ns=self._exit_at(ctx, b, hi_m, lo_m, e, Direction.LONG, True)
                    ),
                    replace(
                        short,
                        exit_at_ns=self._exit_at(ctx, b, hi_m, lo_m, e, Direction.SHORT, True),
                    ),
                )
            )
        return out

    def neutral(
        self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta, kind: NeutralKind, n: int
    ) -> TradeSpec:
        b = ctx.bars("D1", "BID")
        time_exit = _bar_open(b, meta.entry_bar + self.time_exit_days, ctx)
        return _neutral_common(spec, kind, _bar_open(b, meta.entry_bar + n, ctx), time_exit)

    def rollover_days(self, ctx: Context, entry_ns: int, exit_ns: int) -> list[Any]:
        b = ctx.bars("D1", "BID")
        closes = b.close_ns[(b.close_ns > entry_ns) & (b.close_ns <= exit_ns)]
        return [from_ns(int(t)).date() for t in closes]


# ------------------------------------------------------------------------------ S3


class SundayGap:
    """S3 ``s3-sungap-dev-001``: fade the Sunday reopening gap towards Friday's close."""

    random_direction = False

    time_exit_ns = 48 * HOUR_NS

    def candidates(self, ctx: Context, v: Variant) -> Candidates:
        assert v.k is not None and v.d is not None and v.s is not None
        _, _, _, mc = price_columns(ctx.path, "MID")
        out: Candidates = []
        for w in ctx.weekends():
            if w.status != "ok" or abs(w.gap) < v.k * w.atr20 or w.gap == 0.0:
                continue
            avail = w.reopen_ns + v.d * MIN_NS
            i0, i1 = minute_index(ctx.path, w.reopen_ns, avail)
            if i1 == i0 or not ctx.in_split(avail):
                continue
            ref = float(mc[i1 - 1])  # last mid known at the entry instant
            d = Direction.SHORT if w.gap > 0 else Direction.LONG
            half = w.c + w.gap / 2.0
            if (d is Direction.SHORT and ref <= half) or (d is Direction.LONG and ref >= half):
                continue  # half the gap or more already closed: no trade
            spec = _spec(
                _key(ctx, v, d, avail),
                ctx,
                d,
                ref,
                v.s * w.atr20,
                None,
                avail,
                avail + _ENTRY_WINDOW_NS,
                avail + self.time_exit_ns,
                target_price=w.c,
            )
            out.append((spec, Meta(-1, w.atr20, {"gap": w.gap, "friday_close": w.c})))
        out.sort(key=lambda x: (x[0].available_at_ns, x[0].key))
        return out

    def mirror(self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta) -> None:
        return None  # random_direction does not apply: the target is on one side only

    def pool(self, ctx: Context, v: Variant, ratio: float) -> None:
        return None

    def neutral(
        self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta, kind: NeutralKind, n: int
    ) -> TradeSpec:
        return _neutral_common(spec, kind, spec.available_at_ns + n * HOUR_NS, None)


# ------------------------------------------------------------------------------ S7


class IntradaySeason:
    """S7 ``s7-intraday-dev-001``: fixed-direction session legs (Breedon-Ranaldo)."""

    random_direction = True

    atr_n = 20

    def legs(self, ctx: Context, v: Variant) -> list[str]:
        assert v.legs is not None
        return [HOME[ctx.symbol] if leg == "home" else "ny" for leg in v.legs]

    def candidates(self, ctx: Context, v: Variant) -> Candidates:
        d1 = ctx.bars("D1", "MID")
        a20 = atr(d1, self.atr_n)
        _, _, _, mc = price_columns(ctx.path, "MID")
        out: Candidates = []
        for day in london_weekdays(ctx.start_ns, ctx.end_ns):
            for name in self.legs(ctx, v):
                h0, h1 = SESSIONS[name]
                w0, w1 = london_window(day, h0, h1)
                if w0 < ctx.start_ns or w1 > ctx.end_ns:
                    continue
                i0, i1 = minute_index(ctx.path, w0, w1)
                if i1 - i0 != (w1 - w0) // MIN_NS:
                    continue  # no entry on days with holes in the session data (rule 5)
                k = int(np.searchsorted(d1.close_ns, w0, side="right")) - 1
                if k < 0 or math.isnan(a20[k]) or i0 == 0:
                    continue
                d = Direction.LONG if PREDICTED[ctx.symbol][name] > 0 else Direction.SHORT
                ref = float(mc[i0 - 1])
                spec = _spec(
                    _key(ctx, v, d, w0, f":{name}"),
                    ctx,
                    d,
                    ref,
                    float(a20[k]),
                    None,
                    w0,
                    w0 + _ENTRY_WINDOW_NS,
                    w1,
                )
                out.append((spec, Meta(-1, float(a20[k]), {"session": name})))
        out.sort(key=lambda x: (x[0].available_at_ns, x[0].key))
        return out

    def mirror(self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta) -> Pair:
        return _both(spec, spec.risk, None, f"bench:{spec.key}")

    def pool(self, ctx: Context, v: Variant, ratio: float) -> None:
        return None

    def neutral(
        self, ctx: Context, v: Variant, spec: TradeSpec, meta: Meta, kind: NeutralKind, n: int
    ) -> None:
        return None


RULES: dict[str, Rule] = {
    "s1_failure_test": FailureTest(),
    "s2_daily_trend": DailyTrend(),
    "s3_sunday_gap": SundayGap(),
    "s7_intraday": IntradaySeason(),
}
