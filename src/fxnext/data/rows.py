"""Observed M15 rows, rejected rows and quality counters produced by importers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from fxnext.domain.enums import Availability, RejectReason, SlotStatus

ROW_COLUMNS = (
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
class RowDraft:
    open_time_ns: int
    available_at_ns: int  # for MODELLED rows: close_time (publication_delay applied later)
    availability: Availability
    status: SlotStatus  # VALID or FLAT_ZERO_VOLUME
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Rejected:
    open_time_ns: int
    available_at_ns: int
    reason: RejectReason
    detail: str


@dataclass(slots=True)
class ImportResult:
    rows: list[RowDraft] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    quality: Counter[str] = field(default_factory=Counter)

    def columns(self) -> dict[str, npt.NDArray[np.generic]]:
        ordered = sorted(self.rows, key=lambda r: r.open_time_ns)
        return {
            "open_time_ns": np.array([r.open_time_ns for r in ordered], dtype="<i8"),
            "available_at_ns": np.array([r.available_at_ns for r in ordered], dtype="<i8"),
            "availability": np.array([int(r.availability) for r in ordered], dtype="u1"),
            "status": np.array([int(r.status) for r in ordered], dtype="u1"),
            "open": np.array([r.open for r in ordered], dtype="<f8"),
            "high": np.array([r.high for r in ordered], dtype="<f8"),
            "low": np.array([r.low for r in ordered], dtype="<f8"),
            "close": np.array([r.close for r in ordered], dtype="<f8"),
            "volume": np.array([r.volume for r in ordered], dtype="<f8"),
        }

    def sorted_rejected(self) -> list[Rejected]:
        return sorted(self.rejected, key=lambda r: (r.open_time_ns, r.reason.value, r.detail))


def check_ohlc(o: float, h: float, lo: float, c: float) -> RejectReason | None:
    values = (o, h, lo, c)
    if not all(np.isfinite(v) for v in values):
        return RejectReason.NON_FINITE
    if min(values) <= 0:
        return RejectReason.NON_POSITIVE
    if h < max(o, c) or lo > min(o, c) or lo > h:
        return RejectReason.OHLC_INCONSISTENT
    return None


def is_flat_zero(o: float, h: float, lo: float, c: float, volume: float) -> bool:
    return o == h == lo == c and volume == 0.0
