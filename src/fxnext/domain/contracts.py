"""Pydantic contracts of contracts-v0.2 (snapshot, strategy, evaluation)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import model_validator

from fxnext.domain.enums import (
    Comparator,
    DatasetProvenance,
    Direction,
    FilterKind,
    FilterStatus,
    PriceBasis,
    Provenance,
    ReasonCode,
    SlotStatus,
    Timeframe,
)
from fxnext.domain.frozen import FrozenMap
from fxnext.domain.hashing import content_hash
from fxnext.domain.model import FrozenJson, Model, UtcDatetime
from fxnext.domain.series import BarSeries
from fxnext.domain.timeutil import to_ns


class SnapshotInvariantError(ValueError):
    pass


class DuplicateEventError(ValueError):
    pass


class Instrument(Model):
    symbol: str
    base: str
    quote: str
    digits: int
    point: Decimal


INSTRUMENTS: FrozenMap[str, Instrument] = FrozenMap(
    {
        "EURUSD": Instrument(
            symbol="EURUSD", base="EUR", quote="USD", digits=5, point=Decimal("0.00001")
        ),
        "GBPUSD": Instrument(
            symbol="GBPUSD", base="GBP", quote="USD", digits=5, point=Decimal("0.00001")
        ),
        "USDJPY": Instrument(
            symbol="USDJPY", base="USD", quote="JPY", digits=3, point=Decimal("0.001")
        ),
    }
)


class Slot(Model):
    timeframe: Timeframe
    open_time: UtcDatetime
    close_time: UtcDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.close_time - self.open_time != self.timeframe.duration:
            raise ValueError("slot close_time must be open_time + duration")
        return self


class Quote(Model):
    symbol: str
    time: UtcDatetime
    bid: float
    ask: float
    provenance: Provenance


class DatasetRef(Model):
    dataset_id: str
    symbol: str
    timeframe: Timeframe
    basis: PriceBasis
    provenance: DatasetProvenance
    coverage_start: UtcDatetime
    coverage_end: UtcDatetime


class SnapshotRequirements(Model):
    timeframe: Timeframe
    basis: PriceBasis
    slots_back: int


class MarketSnapshot(Model):
    hash_exclude: ClassVar[frozenset[str]] = frozenset({"snapshot_hash"})

    symbol: str
    instrument: Instrument
    as_of: UtcDatetime
    signal_slot: Slot
    next_slot: Slot | None
    calendar_version: str
    series: FrozenMap[Timeframe, BarSeries]
    quote: Quote | None
    sources: tuple[DatasetRef, ...]
    snapshot_hash: str = ""

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        as_of_ns = to_ns(self.as_of)
        for series in self.series.values():
            if series.symbol != self.symbol:
                raise SnapshotInvariantError("series symbol mismatch")
            if len(series) and int(series.close_time_ns.max()) > as_of_ns:
                raise SnapshotInvariantError("slot closing after as_of in snapshot")
            priced = series.status <= SlotStatus.FLAT_ZERO_VOLUME
            if priced.any() and int(series.available_at_ns[priced].max()) > as_of_ns:
                raise SnapshotInvariantError("bar with available_at > as_of in snapshot")
        expected = content_hash("MarketSnapshot/1", self)
        if self.snapshot_hash == "":
            object.__setattr__(self, "snapshot_hash", expected)
        elif self.snapshot_hash != expected:
            raise SnapshotInvariantError("snapshot_hash does not match content")
        return self


class StrategyParams(Model):
    """Base class for strategy parameter models (only immutable fields)."""


class StrategyConfig(Model):
    hash_exclude: ClassVar[frozenset[str]] = frozenset({"config_hash"})

    strategy_id: str
    strategy_version: str
    params: StrategyParams
    detection_param_names: tuple[str, ...]
    config_hash: str = ""

    @model_validator(mode="after")
    def _hash(self) -> Self:
        for name in self.detection_param_names:
            if name not in type(self.params).model_fields:
                raise ValueError(f"unknown detection param {name}")
        expected = content_hash("StrategyConfig/1", self)
        if self.config_hash == "":
            object.__setattr__(self, "config_hash", expected)
        elif self.config_hash != expected:
            raise ValueError("config_hash does not match content")
        return self

    def detection_params(self) -> FrozenMap[str, object]:
        return FrozenMap({n: getattr(self.params, n) for n in self.detection_param_names})


class StrategyState(Model):
    hash_exclude: ClassVar[frozenset[str]] = frozenset({"state_hash"})

    schema_version: str
    payload: FrozenJson = None
    state_hash: str = ""

    @model_validator(mode="after")
    def _hash(self) -> Self:
        object.__setattr__(self, "state_hash", content_hash("StrategyState/1", self))
        return self


EMPTY_STATE = StrategyState(schema_version="empty-v1")


class FilterResult(Model):
    name: str
    kind: FilterKind
    status: FilterStatus
    measure: FrozenJson = None
    threshold: FrozenJson = None
    comparator: Comparator | None = None
    reason_code: ReasonCode | None = None

    @model_validator(mode="after")
    def _reason(self) -> Self:
        if (self.status is FilterStatus.FAILED) != (self.reason_code is not None):
            raise ValueError("reason_code must be set iff status is FAILED")
        return self


class StructuralEvent(Model):
    event_id: str
    kind: str
    timeframe: Timeframe
    anchor_open_time: UtcDatetime
    direction: Direction
    discriminator: FrozenJson
    details: FrozenJson = None


class Candidate(Model):
    candidate_id: str
    event_id: str
    direction: Direction
    level_basis: PriceBasis
    entry_ref: float
    stop: float
    target: float
    available_at: UtcDatetime
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _geometry(self) -> Self:
        if self.direction is Direction.LONG:
            ok = self.stop < self.entry_ref < self.target
        else:
            ok = self.target < self.entry_ref < self.stop
        if not ok or min(self.stop, self.target) <= 0:
            raise ValueError("candidate geometry inconsistent with direction")
        if self.expires_at <= self.available_at:
            raise ValueError("expires_at must be after available_at")
        return self


class Abstention(Model):
    reason_code: ReasonCode
    detail: str


class EvaluationRequest(Model):
    hash_exclude: ClassVar[frozenset[str]] = frozenset({"request_id"})

    strategy_id: str
    strategy_version: str
    config_hash: str
    symbol: str
    signal_timeframe: Timeframe
    expected_close: UtcDatetime
    as_of: UtcDatetime
    scheduler_version: str
    request_id: str = ""

    @model_validator(mode="after")
    def _hash(self) -> Self:
        if self.as_of < self.expected_close:
            raise ValueError("as_of must be >= expected_close")
        object.__setattr__(self, "request_id", content_hash("EvaluationRequest/1", self))
        return self


class StrategyEvaluation(Model):
    hash_exclude: ClassVar[frozenset[str]] = frozenset({"evaluation_hash"})

    request: EvaluationRequest
    engine_version: str
    snapshot_hash: str
    events: tuple[StructuralEvent, ...]
    outcome: Candidate | Abstention
    filters: tuple[FilterResult, ...]
    measures: FrozenMap[str, float]
    next_state: StrategyState
    evaluation_hash: str = ""

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        ids = [e.event_id for e in self.events]
        if len(ids) != len(set(ids)):
            raise DuplicateEventError("duplicate event_id in one evaluation")
        failed = [f for f in self.filters if f.status is FilterStatus.FAILED]
        if failed:
            first = next(i for i, f in enumerate(self.filters) if f.status is FilterStatus.FAILED)
            if any(f.status is not FilterStatus.NOT_EVALUATED for f in self.filters[first + 1 :]):
                raise ValueError("filters after the first failure must be NOT_EVALUATED")
            if not isinstance(self.outcome, Abstention) or (
                self.outcome.reason_code is not failed[0].reason_code
            ):
                raise ValueError("abstention must carry the first failed filter reason")
        object.__setattr__(self, "evaluation_hash", content_hash("StrategyEvaluation/1", self))
        return self


class ScheduleRecord(Model):
    request_id: str
    dispatch: Literal["REPLAY", "DISPATCHED", "NOT_DISPATCHED_LATE", "MISSED_DOWNTIME"]
    executed_at: UtcDatetime | None


__all__ = [
    "Abstention",
    "Candidate",
    "DatasetRef",
    "DuplicateEventError",
    "EMPTY_STATE",
    "EvaluationRequest",
    "FilterResult",
    "INSTRUMENTS",
    "Instrument",
    "MarketSnapshot",
    "Quote",
    "ScheduleRecord",
    "Slot",
    "SnapshotInvariantError",
    "SnapshotRequirements",
    "StrategyConfig",
    "StrategyEvaluation",
    "StrategyParams",
    "StrategyState",
    "StructuralEvent",
    "date",
]
