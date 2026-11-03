"""Slot-aligned columnar bar series, deeply immutable (contracts §4, ADR-0012)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from fxnext.domain.enums import Availability, PriceBasis, SlotStatus, Timeframe

INT64_MIN = np.iinfo(np.int64).min

_COLUMNS: dict[str, np.dtype[Any]] = {
    "open_time_ns": np.dtype("<i8"),
    "close_time_ns": np.dtype("<i8"),
    "available_at_ns": np.dtype("<i8"),
    "availability": np.dtype("u1"),
    "status": np.dtype("u1"),
    "open": np.dtype("<f8"),
    "high": np.dtype("<f8"),
    "low": np.dtype("<f8"),
    "close": np.dtype("<f8"),
    "volume": np.dtype("<f8"),
    "source_count": np.dtype("u1"),
}
COLUMN_NAMES = tuple(_COLUMNS)
_PRICED = (SlotStatus.VALID, SlotStatus.FLAT_ZERO_VOLUME)


class SeriesInvariantError(ValueError):
    pass


def _readonly(buffer: bytes, dtype: np.dtype[Any]) -> npt.NDArray[Any]:
    # An array over an immutable ``bytes`` buffer cannot be made writeable again.
    return np.frombuffer(buffer, dtype=dtype)


class BarSeries:
    """One row per expected slot of a window, ascending open_time."""

    __slots__ = ("symbol", "timeframe", "basis", "_buffers", "_len")

    symbol: str
    timeframe: Timeframe
    basis: PriceBasis
    _buffers: dict[str, bytes]
    _len: int

    def __init__(
        self,
        symbol: str,
        timeframe: Timeframe,
        basis: PriceBasis,
        columns: Mapping[str, npt.ArrayLike],
    ) -> None:
        if set(columns) != set(_COLUMNS):
            missing = set(_COLUMNS) ^ set(columns)
            raise SeriesInvariantError(f"columns mismatch: {sorted(missing)}")
        buffers: dict[str, bytes] = {}
        length: int | None = None
        for name, dtype in _COLUMNS.items():
            arr = np.array(columns[name], dtype=dtype, copy=True, order="C")  # defensive copy
            if arr.ndim != 1:
                raise SeriesInvariantError(f"{name} must be 1-D")
            if length is None:
                length = arr.size
            elif arr.size != length:
                raise SeriesInvariantError("columns have different lengths")
            buffers[name] = arr.tobytes()
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "_buffers", buffers)
        object.__setattr__(self, "_len", length or 0)
        self._validate()

    # -- immutability -----------------------------------------------------
    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("BarSeries is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("BarSeries is immutable")

    def column(self, name: str) -> npt.NDArray[Any]:
        return _readonly(self._buffers[name], _COLUMNS[name])

    def __getattr__(self, name: str) -> npt.NDArray[Any]:
        if name in _COLUMNS:
            return self.column(name)
        raise AttributeError(name)

    def __len__(self) -> int:
        return int(self._len)

    # -- invariants -------------------------------------------------------
    def _validate(self) -> None:
        opens = self.column("open_time_ns")
        if opens.size and np.any(np.diff(opens) <= 0):
            raise SeriesInvariantError("open_time_ns must be strictly increasing")
        if np.any(self.column("close_time_ns") != opens + self.timeframe.ns):
            raise SeriesInvariantError("close_time_ns must equal open_time_ns + duration")
        status = self.column("status")
        if np.any(status > max(SlotStatus)):
            raise SeriesInvariantError("unknown status")
        priced = np.isin(status, [int(s) for s in _PRICED])
        o, h, lo, c = (self.column(n) for n in ("open", "high", "low", "close"))
        if priced.any():
            po, ph, pl, pc = o[priced], h[priced], lo[priced], c[priced]
            if not (
                np.isfinite(po).all()
                and np.isfinite(ph).all()
                and np.isfinite(pl).all()
                and np.isfinite(pc).all()
            ):
                raise SeriesInvariantError("priced rows must have finite prices")
            if (pl <= 0).any():
                raise SeriesInvariantError("prices must be > 0")
            if (ph < np.maximum(po, pc)).any() or (pl > np.minimum(po, pc)).any():
                raise SeriesInvariantError("OHLC inconsistent")
            avail = self.column("available_at_ns")[priced]
            if (avail < self.column("close_time_ns")[priced]).any():
                raise SeriesInvariantError("available_at must be >= close_time")
            if np.isin(self.column("availability")[priced], [0, 1], invert=True).any():
                raise SeriesInvariantError("priced rows need OBSERVED or MODELLED availability")
        unpriced = ~priced
        if unpriced.any():
            for arr in (o, h, lo, c):
                if not np.isnan(arr[unpriced]).all():
                    raise SeriesInvariantError("unpriced rows must have NaN prices")

    # -- helpers ----------------------------------------------------------
    def statuses(self) -> tuple[SlotStatus, ...]:
        return tuple(SlotStatus(int(s)) for s in self.column("status"))

    def availability_of(self, index: int) -> Availability:
        return Availability(int(self.column("availability")[index]))

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "basis": self.basis.value,
            "rows": int(self._len),
            "columns": {name: hashlib.sha256(self._buffers[name]).hexdigest() for name in _COLUMNS},
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BarSeries):
            return NotImplemented
        return (self.symbol, self.timeframe, self.basis, self._buffers) == (
            other.symbol,
            other.timeframe,
            other.basis,
            other._buffers,
        )

    def __hash__(self) -> int:
        return hash(
            (self.symbol, self.timeframe, self.basis, tuple(self.canonical()["columns"].values()))
        )

    def __repr__(self) -> str:
        return f"BarSeries({self.symbol}, {self.timeframe}, {self.basis}, rows={self._len})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.is_instance_schema(cls)
