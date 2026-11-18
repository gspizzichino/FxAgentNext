"""Monthly long-short currency portfolio (frozen text g10-cm-dev-001, §3-§5).

Columns follow ``CURRENCIES`` (USD first). Weights are +1/k on the k currencies
ranked highest and -1/k on the k ranked lowest; a weight on USD has no exchange
rate exposure and no interest differential, so it contributes nothing.

Period i runs from rebalance i to rebalance i+1 and its return belongs to the
month of rebalance i+1. Costs of the trades at rebalance i are charged to period i.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.research.g10.panel import Month, Panel, add_months, pip_size
from fxnext.research.g10.prereg import CURRENCIES, PAIRS, G10Costs, G10Variant

F64 = npt.NDArray[np.float64]
NC = len(CURRENCIES)
_USD = 0


@dataclass
class Market:
    """Per-period market arrays shared by every variant and benchmark."""

    months: list[Month]  # month each period's return belongs to
    start_month: list[Month]  # month of the opening rebalance
    contiguous: npt.NDArray[np.bool_]  # opening rebalance is in the previous month
    days: F64  # calendar days in the period
    fx: F64  # (P, NC) exchange rate return of each currency against USD
    carry: F64  # (P, NC) (r_c - r_usd) * days / 360, rates known at the opening
    half_spread: F64  # (P, NC) half spread / mid at the opening rebalance
    fee_rel: F64  # (P, NC) (commission + slippage) / mid at the opening, x1
    eligible: npt.NDArray[np.bool_]  # (P, NC) rate known at the opening (USD included)
    rate_at_open: F64  # (P, NC) NaN when unknown
    rebal_rows: list[int]  # row of each opening rebalance in the panel
    zero_spread_filled: dict[str, int] = field(default_factory=dict)  # cost v2 only


def _ccy_value(panel: Panel, ccy: str, row: int) -> float:
    """USD value of one unit of ``ccy`` at the mid close of ``row``."""

    if ccy == "USD":
        return 1.0
    mid = panel.mid(ccy, row)
    return mid if PAIRS[ccy].startswith(ccy) else 1.0 / mid


def build_market(
    panel: Panel, first_month: Month, last_month: Month, costs: G10Costs, rate_lag: int
) -> Market:
    """Periods whose return month lies in [first_month, last_month]."""

    reb = sorted(panel.rebalance.items())
    opens = [
        i
        for i in range(len(reb) - 1)
        if first_month <= reb[i + 1][0] <= last_month and reb[i][0] >= add_months(first_month, -1)
    ]
    p = len(opens)
    out = Market(
        months=[],
        start_month=[],
        contiguous=np.zeros(p, dtype=bool),
        days=np.zeros(p),
        fx=np.zeros((p, NC)),
        carry=np.zeros((p, NC)),
        half_spread=np.zeros((p, NC)),
        fee_rel=np.zeros((p, NC)),
        eligible=np.zeros((p, NC), dtype=bool),
        rate_at_open=np.full((p, NC), np.nan),
        rebal_rows=[],
    )
    fee_pips = costs.commission_pips + costs.slippage_pips
    for j, i in enumerate(opens):
        (m0, r0), (m1, r1) = reb[i], reb[i + 1]
        out.months.append(m1)
        out.start_month.append(m0)
        out.rebal_rows.append(r0)
        out.contiguous[j] = add_months(m0, 1) == m1
        days = float((panel.days[r1] - panel.days[r0]).days)
        out.days[j] = days
        known = add_months(m0, -rate_lag)
        usd_rate = panel.rate("USD", known)
        for c, ccy in enumerate(CURRENCIES):
            rate = panel.rate(ccy, known)
            if rate is not None:
                out.rate_at_open[j, c] = rate
            out.eligible[j, c] = rate is not None and usd_rate is not None
            if ccy == "USD":
                continue
            out.fx[j, c] = _ccy_value(panel, ccy, r1) / _ccy_value(panel, ccy, r0) - 1.0
            if rate is not None and usd_rate is not None:
                out.carry[j, c] = (rate - usd_rate) / 100.0 * days / 360.0
            bid, ask = float(panel.bid[ccy][r0]), float(panel.ask[ccy][r0])
            mid = (bid + ask) / 2.0
            out.half_spread[j, c] = (ask - bid) / 2.0 / mid
            out.fee_rel[j, c] = fee_pips * pip_size(PAIRS[ccy]) / mid
    out.zero_spread_filled = {}
    if costs.fill_zero_spread:
        for c in range(1, NC):
            column = out.half_spread[:, c]
            zero = column <= 0
            positive = column[~zero]
            if zero.any() and positive.size:
                column[zero] = float(np.median(positive))
                out.zero_spread_filled[CURRENCIES[c]] = int(zero.sum())
    if not out.eligible[:, _USD].all():
        out.eligible[~out.eligible[:, _USD], :] = False
    return out


def momentum(panel: Panel, month: Month, lookback: int) -> F64 | None:
    """FX-only return of each currency against USD over ``lookback`` rebalances back."""

    past = panel.rebalance.get(add_months(month, -lookback))
    now = panel.rebalance.get(month)
    if past is None or now is None:
        return None
    return np.array(
        [_ccy_value(panel, c, now) / _ccy_value(panel, c, past) - 1.0 for c in CURRENCIES]
    )


def _order(signal: F64, eligible: npt.NDArray[np.bool_]) -> list[int]:
    """Indices of eligible currencies, best first; ties by currency code."""

    idx = [c for c in range(NC) if eligible[c]]
    return sorted(idx, key=lambda c: (-float(signal[c]), CURRENCIES[c]))


def target_weights(
    panel: Panel, market: Market, variant: G10Variant
) -> tuple[F64, npt.NDArray[np.bool_], list[str]]:
    """(P, NC) weights; ``active`` is False where the variant stays flat; reasons."""

    p = len(market.months)
    weights = np.zeros((p, NC))
    active = np.zeros(p, dtype=bool)
    reasons: list[str] = []
    k = variant.k
    for j in range(p):
        elig = market.eligible[j]
        tag = f"{market.months[j][0]}-{market.months[j][1]:02d}"
        if elig.sum() < 2 * k:
            reasons.append(f"{tag}: {int(elig.sum())} currencies with a known rate")
            continue
        carry = market.rate_at_open[j]
        if variant.signal == "carry":
            order = _order(carry, elig)
        else:
            assert variant.lookback_months is not None
            mom = momentum(panel, market.start_month[j], variant.lookback_months)
            if mom is None:
                reasons.append(f"{tag}: no rebalance {variant.lookback_months} months back")
                continue
            if variant.signal == "momentum":
                order = _order(mom, elig)
            else:
                rank_c = {c: r for r, c in enumerate(_order(carry, elig))}
                rank_m = {c: r for r, c in enumerate(_order(mom, elig))}
                order = sorted(rank_c, key=lambda c: ((rank_c[c] + rank_m[c]) / 2.0, CURRENCIES[c]))
        for c in order[:k]:
            weights[j, c] = 1.0 / k
        for c in order[-k:]:
            weights[j, c] = -1.0 / k
        active[j] = True
    return weights, active, reasons


@dataclass
class Returns:
    """Per-period components; shapes (..., P)."""

    gross: F64
    fx: F64
    carry: F64
    spread: F64
    fees: F64  # commission + slippage at x1
    markup: F64
    turnover: F64

    stress_all: bool = False  # g10-cost-naga-v1: the multiplier also scales spread and markup

    def net(self, multiplier: float = 1.0, markup: bool = True) -> F64:
        k = multiplier if self.stress_all else 1.0
        out = self.gross - k * self.spread - multiplier * self.fees
        return out - k * self.markup if markup else out


def simulate(market: Market, weights: F64, costs: G10Costs) -> Returns:
    """Vectorised over leading axes: ``weights`` is (..., P, NC)."""

    w = weights.copy()
    w[..., _USD] = 0.0  # USD is cash: no exposure, no trade, no markup
    drifted = np.zeros_like(w)
    drifted[..., 1:, :] = w[..., :-1, :] * (1.0 + market.fx[:-1, :])
    trades = np.abs(w - drifted)
    fx = (w * market.fx).sum(axis=-1)
    carry = (w * market.carry).sum(axis=-1)
    return Returns(
        gross=fx + carry,
        fx=fx,
        carry=carry,
        spread=(trades * market.half_spread).sum(axis=-1),
        fees=(trades * market.fee_rel).sum(axis=-1),
        markup=(np.abs(w) * np.asarray(costs.markup_vector())).sum(axis=-1)
        / 100.0
        * market.days
        / 360.0,
        turnover=trades.sum(axis=-1),
        stress_all=costs.stress_all,
    )


def random_weights(
    market: Market, active: npt.NDArray[np.bool_], k: int, reps: int, rng: np.random.Generator
) -> F64:
    """(reps, P, NC): k long and k short drawn among eligible currencies each period."""

    p = len(market.months)
    draws = rng.random((reps, p, NC))
    draws = np.where(market.eligible[None, :, :], draws, np.inf)
    order = np.argsort(draws, axis=-1, kind="stable")
    w = np.zeros((reps, p, NC))
    rows = np.arange(reps)[:, None, None]
    cols = np.arange(p)[None, :, None]
    w[rows, cols, order[..., :k]] = 1.0 / k
    w[rows, cols, order[..., k : 2 * k]] = -1.0 / k
    w[:, ~active, :] = 0.0
    return w


def reference_weights(market: Market) -> tuple[F64, npt.NDArray[np.bool_]]:
    """Long every eligible non-USD currency against USD, equal weights."""

    elig = market.eligible.copy()
    elig[:, _USD] = False
    n = elig.sum(axis=1)
    active = n > 0
    w = np.where(elig, 1.0 / np.maximum(n, 1)[:, None], 0.0)
    return w, active


def describe(weights: F64) -> list[dict[str, Any]]:
    out = []
    for row in weights:
        out.append(
            {
                "long": [CURRENCIES[c] for c in range(NC) if row[c] > 0],
                "short": [CURRENCIES[c] for c in range(NC) if row[c] < 0],
            }
        )
    return out
