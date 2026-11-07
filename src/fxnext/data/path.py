"""M1 BID+ASK price path used only by the research simulator (M2 spec §2, E1).

A path is an immutable, content-addressed table of in-session minutes for which
both a BID and an ASK candle were observed. Missing minutes are simply absent;
the simulator detects holes from the calendar ordinal of each minute.
The strategy never sees a path: it reads M15 datasets through snapshots only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from fxnext.data import bi5
from fxnext.data.dukascopy import FetchStatus, RawCache, iter_days
from fxnext.data.rows import check_ohlc, is_flat_zero
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.enums import DatasetProvenance, PriceBasis, Timeframe
from fxnext.domain.hashing import content_hash
from fxnext.domain.timeutil import from_ns, to_ns

PATH_SCHEMA = "m1-path-v1"
PATH_COLUMNS = (
    "t_ns",
    "status",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
)
_DTYPES = {"t_ns": "<i8", "status": "u1"} | {c: "<f8" for c in PATH_COLUMNS[2:]}
_MIN = 60_000_000_000
_M15 = Timeframe.M15.ns


class MinuteStatus(IntEnum):
    VALID = 0
    FLAT = 1  # no ticks in the minute (zero volume, o=h=l=c): usable for exits, not for entry


class PathIntegrityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PricePath:
    path_id: str
    symbol: str
    provenance: DatasetProvenance
    coverage_start_ns: int
    coverage_end_ns: int
    columns: dict[str, npt.NDArray[Any]]
    quality: dict[str, int] = field(default_factory=dict)
    ordinal: npt.NDArray[np.int64] = field(default_factory=lambda: np.zeros(0, np.int64))

    def __len__(self) -> int:
        return int(self.columns["t_ns"].size)


def _readonly(arr: npt.ArrayLike, dtype: str) -> npt.NDArray[Any]:
    out = np.array(arr, dtype=dtype, copy=True)
    out.setflags(write=False)
    return out


def _meta(symbol: str, provenance: DatasetProvenance, cov0: int, cov1: int) -> dict[str, Any]:
    return {
        "schema": PATH_SCHEMA,
        "symbol": symbol,
        "provenance": provenance.value,
        "coverage_start_ns": cov0,
        "coverage_end_ns": cov1,
    }


def _identity(meta: dict[str, Any], columns: dict[str, npt.NDArray[Any]]) -> str:
    hashes = {
        name: hashlib.sha256(
            np.ascontiguousarray(columns[name], _DTYPES[name]).tobytes()
        ).hexdigest()
        for name in PATH_COLUMNS
    }
    return content_hash("PricePath/1", {"meta": meta, "columns": hashes})


def session_ordinals(
    t_ns: npt.NDArray[np.int64], cov0: int, cov1: int, calendar: FxCalendar | None = None
) -> npt.NDArray[np.int64]:
    """Ordinal of each minute in the sequence of in-session minutes of the coverage.

    Consecutive in-session minutes differ by exactly 1, also across a weekend, so
    ``ordinal[k] - ordinal[k-1] - 1`` is the number of missing in-session minutes.
    """

    calendar = calendar or FxCalendar()
    grid = calendar.slot_grid(Timeframe.M15, from_ns(cov0), from_ns(cov1))
    slot = t_ns - t_ns % _M15
    idx = np.searchsorted(grid, slot)
    if t_ns.size and (
        np.any(idx >= grid.size) or np.any(grid[np.minimum(idx, grid.size - 1)] != slot)
    ):
        raise PathIntegrityError("path contains a minute outside the session calendar")
    return (idx.astype(np.int64) * 15 + (t_ns % _M15) // _MIN).astype(np.int64)


def build_path(
    symbol: str,
    provenance: DatasetProvenance,
    coverage_start: datetime,
    coverage_end: datetime,
    columns: dict[str, npt.ArrayLike],
    quality: dict[str, int] | None = None,
) -> PricePath:
    cols = {k: _readonly(columns[k], _DTYPES[k]) for k in PATH_COLUMNS}
    t = cols["t_ns"]
    if t.size and (np.any(np.diff(t) <= 0) or np.any(t % _MIN)):
        raise PathIntegrityError("path minutes must be strictly increasing and minute aligned")
    for side in ("bid", "ask"):
        o, h, lo, c = (cols[f"{side}_{x}"] for x in ("open", "high", "low", "close"))
        if np.any(h < np.maximum(o, c)) or np.any(lo > np.minimum(o, c)):
            raise PathIntegrityError(f"{side} OHLC inconsistent")
    cov0, cov1 = to_ns(coverage_start), to_ns(coverage_end)
    meta = _meta(symbol, provenance, cov0, cov1)
    ordinal = session_ordinals(t, cov0, cov1)
    ordinal.setflags(write=False)
    return PricePath(
        _identity(meta, cols), symbol, provenance, cov0, cov1, cols, dict(quality or {}), ordinal
    )


def truncate_path(path: PricePath, end_ns: int) -> PricePath:
    """View of ``path`` limited to minutes before ``end_ns`` (M2 spec §14, split boundary).

    Exits of an experiment are resolved only with prices of its own split: a trade
    still open when the split ends becomes ``END_OF_DATA``. Returns ``path`` itself
    when it already ends by ``end_ns``; otherwise a new, content-addressed path.
    """

    if path.coverage_end_ns <= end_ns:
        return path
    keep = path.columns["t_ns"] < end_ns
    quality = dict(path.quality)
    quality["truncated_from"] = 1
    return build_path(
        path.symbol,
        path.provenance,
        from_ns(path.coverage_start_ns),
        from_ns(max(end_ns, path.coverage_start_ns)),
        {k: path.columns[k][keep] for k in PATH_COLUMNS},
        quality,
    )


def write_path(root: Path, path: PricePath) -> str:
    final = Path(root) / path.path_id
    if final.exists():
        load_path(root, path.path_id)
        return path.path_id
    tmp = Path(root) / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir(parents=True)
    try:
        pq.write_table(
            pa.table({k: path.columns[k] for k in PATH_COLUMNS}), tmp / "minutes.parquet"
        )
        manifest = {
            "path_id": path.path_id,
            **_meta(path.symbol, path.provenance, path.coverage_start_ns, path.coverage_end_ns),
            "rows": len(path),
            "quality": dict(sorted(path.quality.items())),
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        for name in ("minutes.parquet", "manifest.json"):
            with (tmp / name).open("ab") as handle:  # fsync needs write access on Windows
                os.fsync(handle.fileno())
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return path.path_id


def load_path(root: Path, path_id: str) -> PricePath:
    directory = Path(root) / path_id
    manifest = json.loads((directory / "manifest.json").read_text())
    table = pq.read_table(directory / "minutes.parquet")
    columns = {k: table.column(k).to_numpy() for k in PATH_COLUMNS}
    path = build_path(
        manifest["symbol"],
        DatasetProvenance(manifest["provenance"]),
        from_ns(manifest["coverage_start_ns"]),
        from_ns(manifest["coverage_end_ns"]),
        columns,
        manifest.get("quality", {}),
    )
    if path.path_id != path_id:
        raise PathIntegrityError(f"path {path_id} content does not match its id")
    return path


def _minutes(
    cache: RawCache, symbol: str, basis: PriceBasis, start: date, end: date, quality: Counter[str]
) -> tuple[dict[int, bi5.MinuteCandle], list[str]]:
    resolved = cache.resolved()
    out: dict[int, bi5.MinuteCandle] = {}
    hashes: list[str] = []
    for day in iter_days(start, end):
        rec = resolved.get((symbol, basis.value, day.isoformat()))
        if rec is None or rec["status"] != FetchStatus.OK:
            quality[f"{basis.value.lower()}_days_missing"] += 1
            continue
        hashes.append(rec["sha256"])
        day_ns = to_ns(datetime(day.year, day.month, day.day, tzinfo=UTC))
        for c in bi5.decode((cache.root / rec["cache_path"]).read_bytes()):
            if c.second_of_day % 60 or not 0 <= c.second_of_day < 86_400:
                quality["minutes_off_grid"] += 1
                continue
            key = day_ns + c.second_of_day * 1_000_000_000
            if key in out and out[key] != c:
                quality["minutes_conflicting"] += 1
                out.pop(key)
                continue
            out[key] = c
    return out, hashes


def import_path_dukascopy(
    cache: RawCache,
    symbol: str,
    start: date,
    end: date,
    *,
    provenance: DatasetProvenance = DatasetProvenance.REAL,
    calendar: FxCalendar | None = None,
) -> tuple[PricePath, dict[str, list[str]]]:
    """In-session minutes of [start, end] (inclusive UTC days) with both sides observed."""

    calendar = calendar or FxCalendar()
    scale = float(10 ** bi5.PRICE_DIGITS[symbol])
    t0 = datetime(start.year, start.month, start.day, tzinfo=UTC)
    t1 = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    in_session = {int(x) for x in calendar.slot_grid(Timeframe.M15, t0, t1)}
    quality: Counter[str] = Counter()
    bid, bid_raw = _minutes(cache, symbol, PriceBasis.BID, start, end, quality)
    ask, ask_raw = _minutes(cache, symbol, PriceBasis.ASK, start, end, quality)
    rows: list[tuple[int, int, float, float, float, float, float, float, float, float]] = []
    for t in sorted(set(bid) | set(ask)):
        if t - t % _M15 not in in_session:
            continue
        b, a = bid.get(t), ask.get(t)
        if b is None or a is None:
            quality["minutes_one_sided"] += 1
            continue
        if check_ohlc(b.open, b.high, b.low, b.close) or check_ohlc(a.open, a.high, a.low, a.close):
            quality["minutes_invalid"] += 1
            continue
        if a.open < b.open or a.close < b.close:
            quality["minutes_negative_spread"] += 1
            continue
        flat = is_flat_zero(b.open, b.high, b.low, b.close, b.volume) or is_flat_zero(
            a.open, a.high, a.low, a.close, a.volume
        )
        quality["minutes_flat" if flat else "minutes_valid"] += 1
        rows.append(
            (
                t,
                int(MinuteStatus.FLAT if flat else MinuteStatus.VALID),
                b.open / scale,
                b.high / scale,
                b.low / scale,
                b.close / scale,
                a.open / scale,
                a.high / scale,
                a.low / scale,
                a.close / scale,
            )
        )
    quality["in_session_minutes"] = len(in_session) * 15
    quality["in_session_missing"] = quality["in_session_minutes"] - len(rows)
    columns: dict[str, npt.ArrayLike] = {
        name: [r[i] for r in rows] for i, name in enumerate(PATH_COLUMNS)
    }
    path = build_path(symbol, provenance, t0, t1, columns, dict(quality))
    return path, {"BID": bid_raw, "ASK": ask_raw}
