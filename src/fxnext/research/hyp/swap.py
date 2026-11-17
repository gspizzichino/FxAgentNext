"""Estimated overnight swap for S2 (pre-registration P2, exception to E8 of the M2 spec).

swap per night, long base = price * (rate_base - rate_quote - markup) / 365
swap per night, short base = price * (rate_quote - rate_base - markup) / 365

The markup is charged on both sides. A night is a 17:00 New York rollover while
the position is open; the Wednesday rollover counts three nights (spot T+2
settlement over the weekend), as on most FX platforms.

Official policy rates, as effective dates of each change (percent per year).
Sources: Federal Reserve (target range, upper bound; FRED DFEDTARU), ECB
(deposit facility rate; FRED ECBDFR), Bank of England (Bank Rate), Bank of Japan
(rate on the policy-rate balances of current accounts). The table covers only
2014-2020: any other date raises, so a later split cannot use it by accident.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date

RATES_VERSION = "policy-rates-v1"
_COVERED = (date(2014, 1, 1), date(2021, 1, 1))

POLICY_RATES: dict[str, tuple[tuple[date, float], ...]] = {
    "USD": (
        (date(2008, 12, 16), 0.25),
        (date(2015, 12, 17), 0.50),
        (date(2016, 12, 15), 0.75),
        (date(2017, 3, 16), 1.00),
        (date(2017, 6, 15), 1.25),
        (date(2017, 12, 14), 1.50),
        (date(2018, 3, 22), 1.75),
        (date(2018, 6, 14), 2.00),
        (date(2018, 9, 27), 2.25),
        (date(2018, 12, 20), 2.50),
        (date(2019, 8, 1), 2.25),
        (date(2019, 9, 19), 2.00),
        (date(2019, 10, 31), 1.75),
        (date(2020, 3, 4), 1.25),
        (date(2020, 3, 16), 0.25),
    ),
    "EUR": (
        (date(2014, 6, 11), -0.10),
        (date(2014, 9, 10), -0.20),
        (date(2015, 12, 9), -0.30),
        (date(2016, 3, 16), -0.40),
        (date(2019, 9, 18), -0.50),
    ),
    "GBP": (
        (date(2009, 3, 5), 0.50),
        (date(2016, 8, 4), 0.25),
        (date(2017, 11, 2), 0.50),
        (date(2018, 8, 2), 0.75),
        (date(2020, 3, 11), 0.25),
        (date(2020, 3, 19), 0.10),
    ),
    "JPY": (
        (date(2008, 12, 19), 0.10),
        (date(2016, 2, 16), -0.10),
    ),
}


def policy_rate(currency: str, day: date) -> float:
    if not _COVERED[0] <= day < _COVERED[1]:
        raise ValueError(f"policy rate table does not cover {day}")
    table = POLICY_RATES[currency]
    k = bisect_right([d for d, _ in table], day) - 1
    if k < 0:
        raise ValueError(f"no {currency} rate before {day}")
    return table[k][1]


def nights_weight(rollover_day: date) -> int:
    """Rollover at 17:00 New York on ``rollover_day``: Wednesday counts three."""

    return 3 if rollover_day.weekday() == 2 else 1


def swap_price(
    symbol: str, long: bool, price: float, rollover_days: list[date], markup_pct: float
) -> tuple[float, int]:
    """Total swap in price units (positive = received) and the nights it covers."""

    base, quote = symbol[:3], symbol[3:]
    total, nights = 0.0, 0
    for day in rollover_days:
        diff = policy_rate(base, day) - policy_rate(quote, day)
        annual = (diff if long else -diff) - markup_pct
        w = nights_weight(day)
        total += price * annual / 100.0 / 365.0 * w
        nights += w
    return total, nights
