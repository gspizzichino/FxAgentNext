"""Broker candles -> in-memory M15 dataset -> the same snapshot builder as replay.

Only closed candles on the calendar's M15 grid enter the dataset; the candle still
forming is dropped. Each candle becomes available at its close (OBSERVED), so the
snapshot builder applies exactly the point-in-time rules used in research.
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from fxnext.data.snapshot import SnapshotBuilder
from fxnext.data.store import Dataset
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.contracts import DatasetRef, MarketSnapshot
from fxnext.domain.enums import (
    Availability,
    DatasetProvenance,
    PriceBasis,
    SlotStatus,
    Timeframe,
)
from fxnext.domain.timeutil import from_ns
from fxnext.ops.models import RawBar

_NS = 1_000_000_000
_M15 = Timeframe.M15.ns


def _check(o: float, h: float, lo: float, c: float) -> bool:
    vals = (o, h, lo, c)
    return all(np.isfinite(v) and v > 0 for v in vals) and h >= max(o, c) and lo <= min(o, c)


def dataset_from_bars(
    symbol: str, bars: list[RawBar], offset_s: int, now_ns: int, calendar: FxCalendar
) -> Dataset | None:
    """Build a BID M15 dataset from broker candles (``None`` if nothing usable)."""

    rows: dict[int, tuple[Any, ...]] = {}
    rejected: list[int] = []
    for b in bars:
        open_ns = (b.open_server_s - offset_s) * _NS
        if open_ns % _M15 or open_ns + _M15 > now_ns:
            continue  # off-grid or still forming
        if not _check(b.open, b.high, b.low, b.close):
            rejected.append(open_ns)
            continue
        flat = b.open == b.high == b.low == b.close and b.tick_volume == 0
        status = SlotStatus.FLAT_ZERO_VOLUME if flat else SlotStatus.VALID
        rows[open_ns] = (b.open, b.high, b.low, b.close, float(b.tick_volume), int(status))
    if not rows:
        return None
    first = min(rows)
    last_close = max(rows) + _M15
    grid = calendar.slot_grid(Timeframe.M15, from_ns(first), from_ns(last_close))
    in_session = {int(x) for x in grid}
    opens = sorted(t for t in rows if t in in_session)
    if not opens:
        return None
    cols: dict[str, Any] = {
        "open_time_ns": np.array(opens, dtype="<i8"),
        "available_at_ns": np.array([t + _M15 for t in opens], dtype="<i8"),
        "availability": np.full(len(opens), int(Availability.OBSERVED), dtype="u1"),
        "status": np.array([rows[t][5] for t in opens], dtype="u1"),
        "open": np.array([rows[t][0] for t in opens], dtype="<f8"),
        "high": np.array([rows[t][1] for t in opens], dtype="<f8"),
        "low": np.array([rows[t][2] for t in opens], dtype="<f8"),
        "close": np.array([rows[t][3] for t in opens], dtype="<f8"),
        "volume": np.array([rows[t][4] for t in opens], dtype="<f8"),
    }
    rej = np.array(sorted(t for t in rejected if t in in_session), dtype="<i8")
    h = hashlib.sha256()
    for name in sorted(cols):
        h.update(np.ascontiguousarray(cols[name]).tobytes())
    h.update(rej.tobytes())
    ref = DatasetRef(
        dataset_id="mt5-live-" + h.hexdigest(),
        symbol=symbol,
        timeframe=Timeframe.M15,
        basis=PriceBasis.BID,
        provenance=DatasetProvenance.REAL,
        coverage_start=from_ns(opens[0]),
        coverage_end=from_ns(opens[-1] + _M15),
    )
    return Dataset(
        ref=ref,
        columns=cols,
        rejected_open_ns=rej,
        rejected_available_ns=rej + _M15,
        rejected_reason=tuple("OHLC_INCONSISTENT" for _ in rej),
        manifest={"source": "mt5", "offset_s": offset_s},
    )


def snapshot_for(
    dataset: Dataset,
    calendar: FxCalendar,
    tf: Timeframe,
    signal_open_ns: int,
    slots_back: int,
    as_of_ns: int,
) -> MarketSnapshot:
    window = calendar.window_ending_at(tf, signal_open_ns, slots_back)
    nxt = calendar.next_slot_open(tf, signal_open_ns)
    return SnapshotBuilder(dataset).build(tf, window, nxt, as_of_ns)


__all__ = ["dataset_from_bars", "snapshot_for"]
