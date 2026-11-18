"""Daily closes of the 9 USD pairs, month-end rebalance days, rates, and the data check.

Only years up to the end of the chosen split are read, and only rate months
before the end of the split, so a development run cannot touch later data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.data.daily import (
    FRED_SERIES,
    G10_DIGITS,
    DailyCache,
    decode_daily,
    parse_fred_csv,
)
from fxnext.data.dukascopy import FetchStatus
from fxnext.domain.enums import PriceBasis
from fxnext.research.g10.prereg import CURRENCIES, G10_SPLITS, PAIRS, WARMUP_FIRST_YEAR
from fxnext.research.protocol import ProtocolError

Month = tuple[int, int]
F64 = npt.NDArray[np.float64]


def pip_size(symbol: str) -> float:
    return 1e-2 if symbol.endswith("JPY") else 1e-4


def add_months(month: Month, n: int) -> Month:
    index = month[0] * 12 + (month[1] - 1) + n
    return index // 12, index % 12 + 1


def month_range(first: Month, last: Month) -> list[Month]:
    out = [first]
    while out[-1] < last:
        out.append(add_months(out[-1], 1))
    return out


@dataclass
class Panel:
    """One row per weekday; NaN where a pair has no valid candle."""

    days: list[date]
    bid: dict[str, F64]  # by currency (non-USD)
    ask: dict[str, F64]
    rebalance: dict[Month, int]  # month -> row index of the rebalance day
    rates: dict[str, dict[Month, float | None]]
    raw_hashes: list[str]
    rate_hashes: dict[str, str]
    quality: dict[str, Any] = field(default_factory=dict)

    def mid(self, ccy: str, row: int) -> float:
        return float((self.bid[ccy][row] + self.ask[ccy][row]) / 2.0)

    def rate(self, ccy: str, month: Month) -> float | None:
        return self.rates.get(ccy, {}).get(month)


def split_window(split: str) -> tuple[Month, Month, int]:
    """First and last month of the split, and the last calendar year to read."""

    if split == "holdout":
        raise ProtocolError("the holdout is closed: it needs an explicit approval and new code")
    start, end = G10_SPLITS[split]
    last = add_months((end.year, end.month), -1)
    return (start.year, start.month), last, last[0]


def load_panel(cache_root: Path, rates_root: Path, split: str) -> Panel:
    first_month, last_month, last_year = split_window(split)
    cache = DailyCache(cache_root)
    resolved = cache.resolved()
    first_day = date(WARMUP_FIRST_YEAR, 1, 1)
    last_day = date(last_year, 12, 31)
    days = [
        date.fromordinal(o)
        for o in range(first_day.toordinal(), last_day.toordinal() + 1)
        if date.fromordinal(o).weekday() < 5
    ]
    row_of = {d: i for i, d in enumerate(days)}
    bid: dict[str, F64] = {}
    ask: dict[str, F64] = {}
    raw_hashes: list[str] = []
    quality: dict[str, Any] = {"pairs": {}, "files": Counter()}
    for ccy, symbol in PAIRS.items():
        sides: dict[str, F64] = {}
        volume: dict[str, F64] = {}
        for basis in (PriceBasis.BID, PriceBasis.ASK):
            close = np.full(len(days), np.nan)
            vol = np.zeros(len(days))
            for year in range(WARMUP_FIRST_YEAR, last_year + 1):
                rec = resolved.get((symbol, basis.value, year))
                if rec is None:
                    quality["files"]["missing"] += 1
                    continue
                if rec["status"] != FetchStatus.OK:
                    quality["files"][rec["status"].lower()] += 1
                    continue
                quality["files"]["ok"] += 1
                raw_hashes.append(rec["sha256"])
                payload = (cache.root / rec["cache_path"]).read_bytes()
                for c in decode_daily(payload, year, G10_DIGITS[symbol]):
                    row = row_of.get(c.day)
                    if row is not None:
                        close[row] = c.close
                        vol[row] = c.volume
            sides[basis.value] = close
            volume[basis.value] = vol
        b, a = sides["BID"], sides["ASK"]
        missing = np.isnan(b) | np.isnan(a)
        flat = ~missing & ((volume["BID"] <= 0) | (volume["ASK"] <= 0))
        crossed = ~missing & ~flat & ~((b > 0) & (a >= b))
        bad = missing | flat | crossed
        bid[ccy] = np.where(bad, np.nan, b)
        ask[ccy] = np.where(bad, np.nan, a)
        per_year = {}
        for year in range(WARMUP_FIRST_YEAR, last_year + 1):
            mask = np.array([d.year == year for d in days])
            per_year[str(year)] = {
                "weekdays": int(mask.sum()),
                "valid": int((~bad & mask).sum()),
                "missing": int((missing & mask).sum()),
                "zero_volume": int((flat & mask).sum()),
                "crossed": int((crossed & mask).sum()),
            }
        quality["pairs"][symbol] = {"by_year": per_year}
    all_valid = np.ones(len(days), dtype=bool)
    for ccy in PAIRS:
        all_valid &= ~np.isnan(bid[ccy])
    rebalance: dict[Month, int] = {}
    not_last_weekday: list[str] = []
    without: list[str] = []
    for month in month_range((WARMUP_FIRST_YEAR, 1), last_month):
        rows = [row_of[d] for d in days if (d.year, d.month) == month]
        good = [r for r in rows if all_valid[r]]
        if not good:
            without.append(f"{month[0]}-{month[1]:02d}")
            continue
        rebalance[month] = good[-1]
        if good[-1] != rows[-1]:
            not_last_weekday.append(days[good[-1]].isoformat())
    quality["months_without_rebalance"] = without
    quality["rebalance_not_last_weekday"] = not_last_weekday
    spreads: dict[str, dict[str, float]] = {}
    for ccy, symbol in PAIRS.items():
        by_year: dict[str, list[float]] = {}
        for month, row in rebalance.items():
            pips = (ask[ccy][row] - bid[ccy][row]) / pip_size(symbol)
            by_year.setdefault(str(month[0]), []).append(float(pips))
        spreads[symbol] = {y: float(np.median(v)) for y, v in sorted(by_year.items())}
    quality["month_end_spread_pips_median"] = spreads
    rates, rate_hashes, rate_quality = load_rates(rates_root, last_month)
    quality["rates"] = rate_quality
    quality["files"] = dict(quality["files"])
    return Panel(days, bid, ask, rebalance, rates, sorted(raw_hashes), rate_hashes, quality)


def load_rates(
    root: Path, last_month: Month
) -> tuple[dict[str, dict[Month, float | None]], dict[str, str], dict[str, Any]]:
    """One CSV per series, ``{SERIES}.{sha12}.csv``; months after the split are dropped."""

    import hashlib

    rates: dict[str, dict[Month, float | None]] = {}
    hashes: dict[str, str] = {}
    quality: dict[str, Any] = {}
    window = month_range((WARMUP_FIRST_YEAR - 1, 12), last_month)
    for ccy in CURRENCIES:
        sid = FRED_SERIES[ccy]
        files = sorted(Path(root).glob(f"{sid}.*.csv"))
        if len(files) != 1:
            raise ProtocolError(
                f"expected exactly one file for {sid} in {root}, found {len(files)}"
            )
        payload = files[0].read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if not files[0].name.startswith(f"{sid}.{digest[:12]}"):
            raise ProtocolError(f"{files[0].name}: content does not match its hash")
        hashes[sid] = digest
        series = parse_fred_csv(payload, sid)
        kept = {m: v for m, v in series.items() if m <= last_month}
        rates[ccy] = kept
        quality[ccy] = {
            "series": sid,
            "missing_months": [f"{m[0]}-{m[1]:02d}" for m in window if kept.get(m) is None],
        }
    return rates, hashes, quality
