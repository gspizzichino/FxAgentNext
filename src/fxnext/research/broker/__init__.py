"""Measured broker cost profiles (``broker-costs-v1``).

A profile is a JSON snapshot read from a broker terminal, kept in this package and
checked by sha256 on load, so a run that uses it is tied to the exact numbers.
The first profile is the Naga Markets MT5 demo read on 2026-11-18 (read-only probe,
no orders): spread per pair, swap in points per lot per night, zero commission.

Swap model (MT5 ``swap_mode = points``). If the broker pays the interest
differential minus a markup m on both sides,

    swap_long  = diff - m,    swap_short = -diff - m

so ``m = -(swap_long + swap_short) / 2`` and ``diff = (swap_long - swap_short) / 2``,
whatever the rates are. In percent of notional per year:
``points * point * 365 / price`` (the Wednesday triple swap makes 7 charges a week).

The profile does not change any frozen pre-registration: it is used only by a new
one that names it (``g10-cost-naga-v1``) or by explicit calls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

BROKER_COSTS_VERSION = "broker-costs-v1"

# Profile name -> sha256 of its JSON file. A changed file is a new profile.
PROFILES: dict[str, str] = {
    "naga-demo-2026-11-18": "f2435a646eae27b268960c7da1b92b662ae71178ab4dd493a1f465c30d7c06a5",
}


class BrokerProfileError(ValueError):
    pass


@dataclass(frozen=True)
class PairCosts:
    pair: str
    point: float
    swap_long_points: float
    swap_short_points: float
    live_spread_points: tuple[float, ...]
    reference_mid_approx: float

    def pips(self, points: float) -> float:
        """Points to pips: a pip is 0.0001, or 0.01 for JPY quotes."""

        pip = 0.01 if self.pair.endswith("JPY") else 0.0001
        return points * self.point / pip

    def pct_per_year(self, points: float, price: float | None = None) -> float:
        px = self.reference_mid_approx if price is None else price
        if px <= 0:
            raise BrokerProfileError(f"{self.pair}: price must be positive")
        return points * self.point * 365.0 / px * 100.0

    def markup_points(self) -> float:
        return -(self.swap_long_points + self.swap_short_points) / 2.0

    def markup_pct_per_side(self, price: float | None = None) -> float:
        return self.pct_per_year(self.markup_points(), price)

    def implied_differential_pct(self, price: float | None = None) -> float:
        """Base minus quote rate implied by the swaps (percent per year)."""

        return self.pct_per_year((self.swap_long_points - self.swap_short_points) / 2.0, price)

    def swap_pct_per_year(self, long: bool, price: float | None = None) -> float:
        pts = self.swap_long_points if long else self.swap_short_points
        return self.pct_per_year(pts, price)

    def live_spread_pips(self) -> float | None:
        if not self.live_spread_points:
            return None
        s = sorted(self.live_spread_points)
        return self.pips(s[len(s) // 2])


@dataclass(frozen=True)
class BrokerProfile:
    name: str
    sha256: str
    raw: dict[str, Any]

    @property
    def commission_pips_per_side(self) -> float:
        return float(self.raw["commission"]["commission_pips_per_side"])

    @property
    def pairs(self) -> tuple[str, ...]:
        return tuple(sorted(self.raw["symbols"]))

    def pair(self, pair: str) -> PairCosts:
        try:
            s = self.raw["symbols"][pair]
        except KeyError:
            raise BrokerProfileError(f"{self.name}: no pair {pair}") from None
        if s["swap_mode"] != "points":
            raise BrokerProfileError(f"{pair}: swap mode {s['swap_mode']} not supported")
        return PairCosts(
            pair=pair,
            point=float(s["point"]),
            swap_long_points=float(s["swap_long_points"]),
            swap_short_points=float(s["swap_short_points"]),
            live_spread_points=tuple(float(x) for x in s["live_spread_points"]),
            reference_mid_approx=float(s["reference_mid_approx"]),
        )

    def historical_spread_pips(self, pair: str, server_hour: int | None = None) -> float | None:
        """Median spread 2015-2023 (trade-server clock), overall or for one hour.

        Only the pairs exported from the terminal have it (EURUSD, GBPUSD, USDJPY).
        """

        h = self.raw["historical_spread"]["symbols"].get(pair)
        if h is None:
            return None
        pc = self.pair(pair)
        if server_hour is None:
            years = [v for y, v in h["by_year"].items() if not _unreliable(pair, int(y))]
            meds = sorted(v["median"] for v in years)
            return pc.pips(meds[len(meds) // 2]) if meds else None
        if not 0 <= server_hour <= 23:
            raise BrokerProfileError("server_hour must be 0-23")
        return pc.pips(float(h["median_by_server_hour"][str(server_hour)]))

    def spread_pips(self, pair: str, server_hour: int | None = None) -> float | None:
        """Historical median when available, otherwise the live snapshot."""

        hist = self.historical_spread_pips(pair, server_hour)
        return hist if hist is not None else self.pair(pair).live_spread_pips()

    def markup_pct_by_currency(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        """Markup per side of each non-USD G10 currency, from its USD pair."""

        out: dict[str, float] = {}
        for pair in self.pairs:
            if "USD" not in pair:
                continue
            ccy = pair[3:] if pair.startswith("USD") else pair[:3]
            px = None if prices is None else prices.get(pair)
            out[ccy] = round(self.pair(pair).markup_pct_per_side(px), 4)
        return dict(sorted(out.items()))

    def table(self, prices: dict[str, float] | None = None) -> list[dict[str, Any]]:
        rows = []
        for p in self.pairs:
            c = self.pair(p)
            px = None if prices is None else prices.get(p)
            rows.append(
                {
                    "pair": p,
                    "spread_pips": self.spread_pips(p),
                    "live_spread_pips": c.live_spread_pips(),
                    "swap_long_pct": c.swap_pct_per_year(True, px),
                    "swap_short_pct": c.swap_pct_per_year(False, px),
                    "markup_pct_per_side": c.markup_pct_per_side(px),
                    "implied_differential_pct": c.implied_differential_pct(px),
                }
            )
        return rows


def _unreliable(pair: str, year: int) -> bool:
    # Measured on the export: zero spread in 41% of the 2020 bars and 15% of 2021,
    # and 2019 starts on 19 November with a constant spread.
    return pair == "EURUSD" and year in (2019, 2020, 2021)


@cache
def load_profile(name: str = "naga-demo-2026-11-18") -> BrokerProfile:
    if name not in PROFILES:
        raise BrokerProfileError(f"unknown broker profile {name}")
    data = resources.files(__package__).joinpath(f"{name}.json").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != PROFILES[name]:
        raise BrokerProfileError(f"{name}: sha256 {digest} does not match the registered one")
    return BrokerProfile(name, digest, json.loads(data))


__all__ = [
    "BROKER_COSTS_VERSION",
    "PROFILES",
    "BrokerProfile",
    "BrokerProfileError",
    "PairCosts",
    "load_profile",
]
