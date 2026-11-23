from datetime import date

import numpy as np
import pytest

from fxnext.research.gotobi.calendar import control_days, gotobi_days, tokyo_business_day
from fxnext.research.gotobi.run import WINDOWS, assess, net_bp, window_minutes


def test_gotobi_moves_to_previous_tokyo_business_day() -> None:
    days = gotobi_days(date(2024, 1, 1), date(2024, 3, 1))
    # 5 Jan 2024 Fri; 10 Wed; 15 Mon; 20 Sat -> 19 Fri; 25 Thu; 31 Wed
    assert days[:6] == [date(2024, 1, d) for d in (5, 10, 15, 19, 25, 31)]
    # 12 Feb 2024 is a holiday (substitute for National Foundation Day): 10 Feb Sat -> 9 Fri
    assert date(2024, 2, 9) in days
    # 25 Feb 2024 Sun -> 23 Feb is the Emperor's Birthday -> 22 Feb Thu
    assert date(2024, 2, 22) in days and date(2024, 2, 23) not in days
    assert all(tokyo_business_day(d) for d in days)


def test_new_year_and_dec_31_are_not_business_days() -> None:
    assert not tokyo_business_day(date(2025, 1, 2))
    assert not tokyo_business_day(date(2025, 12, 31))
    assert date(2025, 12, 30) in gotobi_days(date(2025, 12, 1), date(2026, 1, 1))


def test_control_days_exclude_gotobi() -> None:
    g = set(gotobi_days(date(2010, 1, 1), date(2011, 1, 1)))
    c = control_days(date(2010, 1, 1), date(2011, 1, 1))
    assert g.isdisjoint(c) and len(g) + len(c) > 230


def test_window_is_0800_to_0955_tokyo() -> None:
    t0, t1 = window_minutes(date(2024, 1, 10))
    assert (t1 - t0) // 60_000_000_000 == 115


def test_windows_skip_seen_years() -> None:
    assert all(not (a.year <= 2015 < b.year or a.year <= 2023 < b.year) for a, b in WINDOWS)


def _row(mid_in: float, mid_out: float, spread: float, gotobi: bool) -> dict:
    h = spread * 0.01 / 2
    return {
        "gotobi": gotobi,
        "day": "2024-01-05",
        "ask_in": mid_in + h,
        "bid_in": mid_in - h,
        "ask_out": mid_out + h,
        "bid_out": mid_out - h,
        "spread_in_pips": spread,
        "spread_out_pips": spread,
    }


def test_net_uses_naga_minimum_spread() -> None:
    r = _row(150.0, 150.0, 0.2, True)  # Dukascopy 0.2 pip each side -> Naga 0.9 rt + 0.2 slip
    assert net_bp(r) == pytest.approx(-1.1 * 0.01 / 150 * 1e4)
    assert net_bp(r, naga=False) == pytest.approx(-0.4 * 0.01 / 150 * 1e4)


def test_assess_outcomes() -> None:
    rng = np.random.default_rng(0)
    rows = [_row(150, 150 * (1 + (5 + rng.normal(0, 3)) / 1e4), 0.5, True) for _ in range(200)]
    rows += [_row(150, 150 * (1 + rng.normal(0, 3) / 1e4), 0.5, False) for _ in range(600)]
    assert assess(rows)["outcome"] == "PASSED"
    kinds = [True] * 200 + [False] * 600
    flat = [_row(150, 150 * (1 + rng.normal(0, 3) / 1e4), 0.5, g) for g in kinds]
    assert assess(flat)["outcome"] == "REJECTED"
