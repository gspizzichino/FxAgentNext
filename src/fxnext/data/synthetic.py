"""Deterministic synthetic M15 datasets for tests and the reproducible example."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import numpy as np

from fxnext.data import bi5
from fxnext.data.rows import ImportResult, Rejected, RowDraft
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.enums import Availability, RejectReason, SlotStatus, Timeframe

_M15 = Timeframe.M15.ns


def synthetic_m15(
    start: datetime,
    end: datetime,
    *,
    seed: int = 7,
    start_points: int = 110_000,
    scale: int = 100_000,
    step_points: int = 12,
    gaps: Iterable[int] = (),
    flats: Iterable[int] = (),
    invalid: Iterable[int] = (),
    late: dict[int, int] | None = None,
) -> ImportResult:
    """Integer-point random walk on every in-session M15 slot of [start, end).

    ``gaps``/``flats``/``invalid`` are M15 open times (ns); ``late`` maps open time to
    an OBSERVED available_at (ns). Integer arithmetic keeps output bit-identical everywhere.
    """

    rng = np.random.Generator(np.random.PCG64(seed))
    grid = FxCalendar().slot_grid(Timeframe.M15, start, end)
    gaps, flats, invalid = set(gaps), set(flats), set(invalid)
    late = late or {}
    price = start_points
    result = ImportResult()
    for open_ns in grid.tolist():
        moves = rng.integers(-step_points, step_points + 1, size=3).tolist()
        o = price
        c = o + moves[0]
        h = max(o, c) + abs(moves[1]) // 2
        lo = min(o, c) - abs(moves[2]) // 2
        price = c
        if open_ns in gaps:
            continue
        close_ns = open_ns + _M15
        if open_ns in invalid:
            result.rejected.append(
                Rejected(open_ns, close_ns, RejectReason.OHLC_INCONSISTENT, "synthetic")
            )
            continue
        avail, kind = close_ns, Availability.MODELLED
        if open_ns in late:
            avail, kind = late[open_ns], Availability.OBSERVED
        if open_ns in flats:
            result.rows.append(
                RowDraft(
                    open_ns,
                    avail,
                    kind,
                    SlotStatus.FLAT_ZERO_VOLUME,
                    o / scale,
                    o / scale,
                    o / scale,
                    o / scale,
                    0.0,
                )
            )
            continue
        volume = float(10 + abs(moves[0]))
        result.rows.append(
            RowDraft(
                open_ns,
                avail,
                kind,
                SlotStatus.VALID,
                o / scale,
                h / scale,
                lo / scale,
                c / scale,
                volume,
            )
        )
    result.quality["in_session_slots"] = int(grid.size)
    return result


def synthetic_bi5_day(
    day: date, *, seed: int, start_points: int = 110_000, weekend_flat: bool = True
) -> bytes:
    """A full day (1440 minutes) of Dukascopy-format candles."""

    rng = np.random.Generator(np.random.PCG64(seed))
    price = start_points
    candles = []
    weekday = day.weekday()
    for minute in range(1440):
        if weekend_flat and weekday == 5:
            candles.append(bi5.MinuteCandle(minute * 60, price, price, price, price, 0.0))
            continue
        step = int(rng.integers(-5, 6))
        o, c = price, price + step
        candles.append(
            bi5.MinuteCandle(minute * 60, o, c, min(o, c) - 1, max(o, c) + 1, float(1 + abs(step)))
        )
        price = c
    return bi5.encode(candles)


def synthetic_raw_cache(
    cache_root: object,
    symbol: str,
    start: date,
    end: date,
    *,
    seed: int = 11,
    spread_points: int = 3,
    start_points: int = 110_000,
) -> None:
    """Fill a raw cache with chained synthetic BID days and ASK = BID + spread (tests, demo)."""

    import hashlib
    from pathlib import Path

    from fxnext.data.dukascopy import FetchStatus, RawCache, iter_days
    from fxnext.domain.enums import PriceBasis

    cache = RawCache(Path(str(cache_root)))
    price = start_points
    for n, day in enumerate(iter_days(start, end)):
        bid = synthetic_bi5_day(day, seed=seed * 1000 + n, start_points=price)
        candles = bi5.decode(bid)
        price = candles[-1].close
        ask = bi5.encode(
            [
                bi5.MinuteCandle(
                    c.second_of_day,
                    c.open + spread_points,
                    c.close + spread_points,
                    c.low + spread_points,
                    c.high + spread_points,
                    c.volume,
                )
                for c in candles
            ]
        )
        for basis, payload in ((PriceBasis.BID, bid), (PriceBasis.ASK, ask)):
            stored = cache.store(symbol, basis, day, payload)
            cache.append(
                {
                    "symbol": symbol,
                    "basis": basis.value,
                    "day": day.isoformat(),
                    "status": FetchStatus.OK,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "cache_path": stored.relative_to(cache.root).as_posix(),
                    "bytes": len(payload),
                    "synthetic": True,
                }
            )


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


__all__ = ["day_start", "synthetic_bi5_day", "synthetic_m15", "synthetic_raw_cache", "timedelta"]
