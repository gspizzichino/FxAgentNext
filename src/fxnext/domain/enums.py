"""Closed enumerations of contracts-v0.2."""

from __future__ import annotations

from datetime import timedelta
from enum import IntEnum, StrEnum


class Timeframe(StrEnum):
    M1 = "M1"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"

    @property
    def duration(self) -> timedelta:
        return _DURATIONS[self]

    @property
    def ns(self) -> int:
        return int(self.duration.total_seconds()) * 1_000_000_000


_DURATIONS = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
}


class PriceBasis(StrEnum):
    BID = "BID"
    ASK = "ASK"
    MID = "MID"


class Provenance(StrEnum):
    OBSERVED = "OBSERVED"
    MODELLED = "MODELLED"


class Availability(IntEnum):
    OBSERVED = 0
    MODELLED = 1
    NOT_APPLICABLE = 255


class DatasetProvenance(StrEnum):
    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"


class SlotStatus(IntEnum):
    """Ordered by severity: the derived status is the max of its sources."""

    VALID = 0
    FLAT_ZERO_VOLUME = 1
    INVALID = 2
    UNAVAILABLE = 3
    BEFORE_COVERAGE = 4


class ReasonCode(StrEnum):
    BAR_NOT_AVAILABLE = "BAR_NOT_AVAILABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DATA_GAP = "DATA_GAP"
    INVALID_DATA = "INVALID_DATA"
    FLAT_BAR_IN_WINDOW = "FLAT_BAR_IN_WINDOW"
    MARKET_CLOSED = "MARKET_CLOSED"
    NO_EVENT = "NO_EVENT"
    FILTER_REJECTED = "FILTER_REJECTED"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Comparator(StrEnum):
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    EQ = "=="
    IN = "in"


class FilterKind(StrEnum):
    DATA_GATE = "DATA_GATE"
    MARKET_GATE = "MARKET_GATE"
    STRATEGY_FILTER = "STRATEGY_FILTER"


class FilterStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_EVALUATED = "NOT_EVALUATED"


class Dispatch(StrEnum):
    REPLAY = "REPLAY"
    DISPATCHED = "DISPATCHED"
    NOT_DISPATCHED_LATE = "NOT_DISPATCHED_LATE"
    MISSED_DOWNTIME = "MISSED_DOWNTIME"


class RejectReason(StrEnum):
    """Why an imported row or interval was not accepted as an observation."""

    OHLC_INCONSISTENT = "OHLC_INCONSISTENT"
    NON_FINITE = "NON_FINITE"
    NON_POSITIVE = "NON_POSITIVE"
    OFF_GRID = "OFF_GRID"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"
    INCOMPLETE_SOURCE = "INCOMPLETE_SOURCE"
    NEGATIVE_SPREAD = "NEGATIVE_SPREAD"
