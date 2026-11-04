"""Runtime-like evaluation loop for the replay/runtime parity test (M2 spec T11).

Bars are not read from a finished dataset: they *arrive* one at a time, in the
order of their (effective) ``available_at``, into an incremental store. At each
scheduled ``as_of`` the loop has received exactly the bars available by then and
builds the snapshot from what it has, as the M3 runtime will from the MT5 feed.
The same calendar schedule and the same strategy are used as in ``run_replay``.

The snapshot cites the same ``DatasetRef`` as the replay, so parity is checked on
the full ``evaluation_hash``: any bar used before its arrival would change it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from fxnext.data.snapshot import SnapshotBuilder
from fxnext.data.store import Dataset
from fxnext.domain.contracts import EMPTY_STATE, StrategyEvaluation
from fxnext.domain.enums import Availability
from fxnext.domain.timeutil import to_ns
from fxnext.replay.runner import build_schedule, window_and_next
from fxnext.strategy.donchian import DonchianBreakout

_COLS = (
    "open_time_ns",
    "available_at_ns",
    "availability",
    "status",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


@dataclass(frozen=True, slots=True)
class Arrival:
    at_ns: int
    open_time_ns: int
    row: int | None  # None: a rejected (invalid) observation


def arrivals(dataset: Dataset, publication_delay_ns: int) -> list[Arrival]:
    c = dataset.columns
    out = []
    for i in range(c["open_time_ns"].size):
        modelled = int(c["availability"][i]) == int(Availability.MODELLED)
        at = int(c["available_at_ns"][i]) + (publication_delay_ns if modelled else 0)
        out.append(Arrival(at, int(c["open_time_ns"][i]), i))
    for t, a in zip(dataset.rejected_open_ns, dataset.rejected_available_ns, strict=True):
        out.append(Arrival(int(a) + publication_delay_ns, int(t), None))
    out.sort(key=lambda x: (x.at_ns, x.open_time_ns, x.row is None))
    return out


class IncrementalSnapshotBuilder(SnapshotBuilder):
    """A ``SnapshotBuilder`` whose store only contains bars that have arrived."""

    def __init__(self, dataset: Dataset, publication_delay_ns: int = 0) -> None:
        super().__init__(dataset, publication_delay_ns)
        self._source = dataset.columns
        self._delay = publication_delay_ns
        self._live_cols: dict[str, list[Any]] = {k: [] for k in _COLS}
        self._live_avail: list[int] = []
        # The base class only indexes these containers, so lists work in place of arrays.
        self._cols = cast(Any, self._live_cols)
        self._eff_avail = cast(Any, self._live_avail)
        self._index = {}
        self._rejected = {}

    def ingest(self, item: Arrival) -> None:
        if item.row is None:
            prev = self._rejected.get(item.open_time_ns)
            self._rejected[item.open_time_ns] = (
                item.at_ns if prev is None else min(prev, item.at_ns)
            )
            return
        if item.open_time_ns in self._index:
            raise ValueError("bar received twice")
        self._index[item.open_time_ns] = len(self._live_avail)
        for k in _COLS:
            self._live_cols[k].append(self._source[k][item.row])
        self._live_avail.append(item.at_ns)


def run_live(
    dataset: Dataset,
    start: datetime,
    end: datetime,
    *,
    evaluation_delay: timedelta = timedelta(0),
    publication_delay: timedelta = timedelta(0),
) -> Iterator[StrategyEvaluation]:
    strategy = DonchianBreakout()
    config = strategy.default_config()
    req = strategy.requirements(config)[0]
    tf = req.timeframe
    delay_ns = int(evaluation_delay.total_seconds() * 1e9)
    pub_ns = int(publication_delay.total_seconds() * 1e9)
    grid, schedule = build_schedule(tf, start, end, delay_ns, [dataset.ref.symbol], req.slots_back)
    builder = IncrementalSnapshotBuilder(dataset, pub_ns)
    feed = arrivals(dataset, pub_ns)
    k = 0
    for as_of_ns, _symbol, idx in schedule:
        while k < len(feed) and feed[k].at_ns <= as_of_ns:
            builder.ingest(feed[k])
            k += 1
        window, nxt = window_and_next(grid, idx, req.slots_back, tf)
        yield strategy.evaluate(builder.build(tf, window, nxt, as_of_ns), config, EMPTY_STATE)


__all__ = ["IncrementalSnapshotBuilder", "arrivals", "run_live", "to_ns"]
