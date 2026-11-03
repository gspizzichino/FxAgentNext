"""Point-in-time snapshot builder with M15 -> H1/H4 derivation (contracts §4, §6)."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.data.store import Dataset
from fxnext.domain.calendar import CALENDAR_VERSION
from fxnext.domain.contracts import INSTRUMENTS, MarketSnapshot, Slot
from fxnext.domain.enums import Availability, SlotStatus, Timeframe
from fxnext.domain.frozen import FrozenMap
from fxnext.domain.series import INT64_MIN, BarSeries
from fxnext.domain.timeutil import from_ns, to_ns

_M15 = Timeframe.M15.ns


class SnapshotBuilder:
    """Builds snapshots from one M15 dataset. Never exposes the dataset to the core."""

    def __init__(self, dataset: Dataset, publication_delay_ns: int = 0) -> None:
        if dataset.ref.timeframe is not Timeframe.M15:
            raise ValueError("builder expects an M15 dataset")
        if publication_delay_ns < 0:
            raise ValueError("publication_delay must be >= 0")
        self.dataset = dataset
        self.delay = publication_delay_ns
        cols = dataset.columns
        self._index = {int(t): i for i, t in enumerate(cols["open_time_ns"])}
        modelled = cols["availability"] == int(Availability.MODELLED)
        self._eff_avail = cols["available_at_ns"] + np.where(modelled, publication_delay_ns, 0)
        self._rejected: dict[int, int] = {}
        for t, a in zip(dataset.rejected_open_ns, dataset.rejected_available_ns, strict=True):
            prev = self._rejected.get(int(t))
            value = int(a) + publication_delay_ns
            self._rejected[int(t)] = value if prev is None else min(prev, value)
        self._cov_start = to_ns(dataset.ref.coverage_start)
        self._cov_end = to_ns(dataset.ref.coverage_end)
        self._cols = cols

    def m15_state(self, open_ns: int, as_of_ns: int) -> tuple[SlotStatus, int | None]:
        if open_ns < self._cov_start:
            return SlotStatus.BEFORE_COVERAGE, None
        if open_ns + _M15 > self._cov_end:
            return SlotStatus.UNAVAILABLE, None
        idx = self._index.get(open_ns)
        if idx is not None and int(self._eff_avail[idx]) <= as_of_ns:
            return SlotStatus(int(self._cols["status"][idx])), idx
        rej = self._rejected.get(open_ns)
        if rej is not None and rej <= as_of_ns:
            return SlotStatus.INVALID, None
        return SlotStatus.UNAVAILABLE, None

    def series(
        self, tf: Timeframe, window_opens: npt.NDArray[np.int64], as_of_ns: int
    ) -> BarSeries:
        if tf.ns % _M15:
            raise ValueError("timeframe must be a multiple of M15")
        ratio = tf.ns // _M15
        n = window_opens.size
        out: dict[str, Any] = {
            "open_time_ns": window_opens,
            "close_time_ns": window_opens + tf.ns,
            "available_at_ns": np.full(n, INT64_MIN, dtype=np.int64),
            "availability": np.full(n, int(Availability.NOT_APPLICABLE), dtype=np.uint8),
            "status": np.zeros(n, dtype=np.uint8),
            "open": np.full(n, np.nan),
            "high": np.full(n, np.nan),
            "low": np.full(n, np.nan),
            "close": np.full(n, np.nan),
            "volume": np.full(n, np.nan),
            "source_count": np.full(n, ratio, dtype=np.uint8),
        }
        c = self._cols
        for i, slot_open in enumerate(window_opens.tolist()):
            states = [self.m15_state(slot_open + k * _M15, as_of_ns) for k in range(ratio)]
            status = max(s for s, _ in states)
            out["status"][i] = int(status)
            if status > SlotStatus.FLAT_ZERO_VOLUME:
                continue
            idxs = [idx for _, idx in states]
            assert all(idx is not None for idx in idxs)
            ii = [int(x) for x in idxs if x is not None]
            high = float(c["high"][ii[0]])
            low = float(c["low"][ii[0]])
            volume = 0.0
            avail = INT64_MIN
            observed = True
            for j in ii:
                high = max(high, float(c["high"][j]))
                low = min(low, float(c["low"][j]))
                volume += float(c["volume"][j])
                avail = max(avail, int(self._eff_avail[j]))
                observed = observed and int(c["availability"][j]) == int(Availability.OBSERVED)
            out["open"][i] = float(c["open"][ii[0]])
            out["close"][i] = float(c["close"][ii[-1]])
            out["high"][i], out["low"][i], out["volume"][i] = high, low, volume
            out["available_at_ns"][i] = avail
            out["availability"][i] = int(
                Availability.OBSERVED if observed else Availability.MODELLED
            )
        return BarSeries(self.dataset.ref.symbol, tf, self.dataset.ref.basis, out)

    def build(
        self,
        tf: Timeframe,
        window_opens: npt.NDArray[np.int64],
        next_open_ns: int | None,
        as_of_ns: int,
    ) -> MarketSnapshot:
        signal_open = int(window_opens[-1])
        symbol = self.dataset.ref.symbol
        return MarketSnapshot(
            symbol=symbol,
            instrument=INSTRUMENTS[symbol],
            as_of=from_ns(as_of_ns),
            signal_slot=Slot(
                timeframe=tf,
                open_time=from_ns(signal_open),
                close_time=from_ns(signal_open + tf.ns),
            ),
            next_slot=None
            if next_open_ns is None
            else Slot(
                timeframe=tf,
                open_time=from_ns(next_open_ns),
                close_time=from_ns(next_open_ns + tf.ns),
            ),
            calendar_version=CALENDAR_VERSION,
            series=FrozenMap({tf: self.series(tf, window_opens, as_of_ns)}),
            quote=None,
            sources=(self.dataset.ref,),
        )
