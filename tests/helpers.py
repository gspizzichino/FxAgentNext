from __future__ import annotations

from datetime import datetime, timedelta

from fxnext.data.snapshot import SnapshotBuilder
from fxnext.data.store import Dataset
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.contracts import EMPTY_STATE, MarketSnapshot, StrategyEvaluation
from fxnext.domain.enums import Timeframe
from fxnext.domain.timeutil import to_ns
from fxnext.strategy.donchian import DonchianBreakout

H1 = Timeframe.H1
STRATEGY = DonchianBreakout()
CONFIG = STRATEGY.default_config()
CAL = FxCalendar()


def snapshot_at(
    dataset: Dataset,
    signal_open: datetime,
    *,
    as_of_delay: timedelta = timedelta(0),
    publication_delay_ns: int = 0,
) -> MarketSnapshot:
    open_ns = to_ns(signal_open)
    window = CAL.window_ending_at(H1, open_ns, 21)
    nxt = CAL.next_slot_open(H1, open_ns)
    as_of = open_ns + H1.ns + int(as_of_delay.total_seconds() * 1e9)
    return SnapshotBuilder(dataset, publication_delay_ns).build(H1, window, nxt, as_of)


def evaluate_at(dataset: Dataset, signal_open: datetime, **kw: object) -> StrategyEvaluation:
    return STRATEGY.evaluate(
        snapshot_at(dataset, signal_open, **kw),
        CONFIG,  # type: ignore[arg-type]
        EMPTY_STATE,
    )
