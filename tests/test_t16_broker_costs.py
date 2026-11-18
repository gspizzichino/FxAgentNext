"""T16: measured broker cost profile (broker-costs-v1) and g10-cost-naga-v1."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from fxnext.research.broker import PROFILES, BrokerProfileError, PairCosts, load_profile
from fxnext.research.g10.portfolio import Returns
from fxnext.research.g10.prereg import CURRENCIES, G10Costs

NAGA = "naga-demo-2026-11-18"


def test_profile_hash_is_checked() -> None:
    prof = load_profile(NAGA)
    assert prof.sha256 == PROFILES[NAGA]
    with pytest.raises(BrokerProfileError):
        load_profile("unknown")


def test_markup_and_differential_from_swaps() -> None:
    # swap_long = diff - m, swap_short = -diff - m, in points per lot per night
    c = PairCosts("USDJPY", 0.001, 2.0, -23.5, (8.0, 8.0, 9.0), 155.0)
    assert c.markup_points() == pytest.approx(10.75)
    assert c.markup_pct_per_side() == pytest.approx(10.75 * 0.001 * 365 / 155 * 100)
    assert c.implied_differential_pct() == pytest.approx(12.75 * 0.001 * 365 / 155 * 100)
    assert c.swap_pct_per_year(True) - c.swap_pct_per_year(False) == pytest.approx(
        2 * c.implied_differential_pct()
    )
    assert c.live_spread_pips() == pytest.approx(0.8)
    # a price passed explicitly replaces the approximate mid
    assert c.markup_pct_per_side(310.0) == pytest.approx(c.markup_pct_per_side() / 2)


def test_naga_numbers() -> None:
    prof = load_profile(NAGA)
    assert prof.commission_pips_per_side == 0.0
    m = prof.markup_pct_by_currency()
    assert set(m) == set(CURRENCIES[1:])
    assert min(m.values()) > 0.8 and max(m.values()) < 3.5
    assert m["CHF"] == max(m.values()) and m["CAD"] == min(m.values())
    # EURUSD 2019-2021 is excluded from the historical median (zero spreads)
    assert prof.historical_spread_pips("EURUSD") == pytest.approx(1.2)
    assert prof.historical_spread_pips("USDCHF") is None
    assert prof.spread_pips("USDCHF") == pytest.approx(1.6)
    assert prof.historical_spread_pips("GBPUSD", 0) > prof.historical_spread_pips("GBPUSD", 12)
    assert prof.pair("AUDJPY").live_spread_pips() is None


def test_g10_naga_costs_validation() -> None:
    m = load_profile(NAGA).markup_pct_by_currency()
    ok = G10Costs(
        version="g10-cost-naga-v1",
        commission_pips=0.0,
        broker_profile=NAGA,
        markup_pct_by_currency=m,
    )
    assert ok.fill_zero_spread and ok.stress_all
    assert ok.markup_vector()[0] == 0.0 and ok.markup_vector()[CURRENCIES.index("CHF")] == m["CHF"]
    with pytest.raises(ValidationError):
        G10Costs(version="g10-cost-naga-v1", broker_profile=NAGA)
    with pytest.raises(ValidationError):
        G10Costs(markup_pct_by_currency=m)
    with pytest.raises(ValidationError):
        G10Costs(
            version="g10-cost-naga-v1",
            broker_profile=NAGA,
            markup_pct_by_currency={k: v for k, v in m.items() if k != "SEK"},
        )
    # frozen versions keep a flat markup and do not stress spread or markup
    v2 = G10Costs(version="g10-cost-v2")
    assert v2.markup_vector() == (0.0,) + (1.0,) * 9 and not v2.stress_all


def test_stress_scales_spread_and_markup_only_for_naga() -> None:
    one = np.array([1.0])
    kw = dict(gross=one * 10, fx=one, carry=one, spread=one, fees=one, markup=one, turnover=one)
    old = Returns(**kw)  # type: ignore[arg-type]
    naga = Returns(**kw, stress_all=True)  # type: ignore[arg-type]
    assert old.net(2.0)[0] == pytest.approx(10 - 1 - 2 - 1)
    assert naga.net(2.0)[0] == pytest.approx(10 - 2 - 2 - 2)
    assert naga.net(1.0)[0] == old.net(1.0)[0]
