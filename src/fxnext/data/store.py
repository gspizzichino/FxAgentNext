"""Immutable datasets: ``root/<dataset_id>/{bars,rejected}.parquet + manifest.json``."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from fxnext.data.rows import ROW_COLUMNS, ImportResult
from fxnext.domain.contracts import DatasetRef
from fxnext.domain.enums import DatasetProvenance, PriceBasis, RejectReason, Timeframe
from fxnext.domain.hashing import content_hash
from fxnext.domain.timeutil import to_ns

DATASET_SCHEMA = "m15-rows-v1"
VALIDATOR_VERSION = "validator-v1"
_DTYPES = {
    "open_time_ns": "<i8",
    "available_at_ns": "<i8",
    "availability": "u1",
    "status": "u1",
    "open": "<f8",
    "high": "<f8",
    "low": "<f8",
    "close": "<f8",
    "volume": "<f8",
}


class DatasetIntegrityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Dataset:
    ref: DatasetRef
    columns: dict[str, npt.NDArray[Any]]
    rejected_open_ns: npt.NDArray[np.int64]
    rejected_available_ns: npt.NDArray[np.int64]
    rejected_reason: tuple[str, ...]
    manifest: dict[str, Any]


def _identity(
    meta: dict[str, Any],
    columns: dict[str, npt.NDArray[Any]],
    rejected: list[tuple[int, int, str, str]],
) -> str:
    col_hashes = {
        name: hashlib.sha256(
            np.ascontiguousarray(columns[name], dtype=_DTYPES[name]).tobytes()
        ).hexdigest()
        for name in ROW_COLUMNS
    }
    return content_hash(
        "Dataset/1", {"meta": meta, "columns": col_hashes, "rejected": [list(r) for r in rejected]}
    )


def write_dataset(
    root: Path,
    result: ImportResult,
    *,
    symbol: str,
    basis: PriceBasis,
    provenance: DatasetProvenance,
    coverage_start: datetime,
    coverage_end: datetime,
    source: dict[str, Any],
) -> DatasetRef:
    columns = result.columns()
    rejected = [
        (r.open_time_ns, r.available_at_ns, r.reason.value, r.detail)
        for r in result.sorted_rejected()
    ]
    meta = {
        "schema": DATASET_SCHEMA,
        "symbol": symbol,
        "timeframe": Timeframe.M15.value,
        "basis": basis.value,
        "provenance": provenance.value,
        "coverage_start_ns": to_ns(coverage_start),
        "coverage_end_ns": to_ns(coverage_end),
    }
    dataset_id = _identity(meta, columns, rejected)
    ref = DatasetRef(
        dataset_id=dataset_id,
        symbol=symbol,
        timeframe=Timeframe.M15,
        basis=basis,
        provenance=provenance,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    final = Path(root) / dataset_id
    if final.exists():
        load_dataset(root, dataset_id)  # verify the existing copy
        return ref
    tmp = Path(root) / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir(parents=True)
    try:
        pq.write_table(pa.table({k: columns[k] for k in ROW_COLUMNS}), tmp / "bars.parquet")
        pq.write_table(
            pa.table(
                {
                    "open_time_ns": pa.array([r[0] for r in rejected], pa.int64()),
                    "available_at_ns": pa.array([r[1] for r in rejected], pa.int64()),
                    "reason": pa.array([r[2] for r in rejected], pa.string()),
                    "detail": pa.array([r[3] for r in rejected], pa.string()),
                }
            ),
            tmp / "rejected.parquet",
        )
        manifest = {
            "dataset_id": dataset_id,
            **meta,
            "validator_version": VALIDATOR_VERSION,
            "rows": int(columns["open_time_ns"].size),
            "rejected": len(rejected),
            "rejected_by_reason": _count(r[2] for r in rejected),
            "quality": dict(sorted(result.quality.items())),
            "source": source,
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        for name in ("bars.parquet", "rejected.parquet", "manifest.json"):
            with (tmp / name).open("ab") as handle:  # fsync needs write access on Windows
                os.fsync(handle.fileno())
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return ref


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items()))


def load_dataset(root: Path, dataset_id: str) -> Dataset:
    directory = Path(root) / dataset_id
    manifest = json.loads((directory / "manifest.json").read_text())
    table = pq.read_table(directory / "bars.parquet")
    columns = {
        k: np.ascontiguousarray(table.column(k).to_numpy(), dtype=_DTYPES[k]) for k in ROW_COLUMNS
    }
    rej = pq.read_table(directory / "rejected.parquet").to_pydict()
    rejected = list(
        zip(rej["open_time_ns"], rej["available_at_ns"], rej["reason"], rej["detail"], strict=True)
    )
    meta = {
        k: manifest[k]
        for k in (
            "schema",
            "symbol",
            "timeframe",
            "basis",
            "provenance",
            "coverage_start_ns",
            "coverage_end_ns",
        )
    }
    if _identity(meta, columns, rejected) != dataset_id:
        raise DatasetIntegrityError(f"dataset {dataset_id} content does not match its id")
    from fxnext.domain.timeutil import from_ns

    ref = DatasetRef(
        dataset_id=dataset_id,
        symbol=meta["symbol"],
        timeframe=Timeframe(meta["timeframe"]),
        basis=PriceBasis(meta["basis"]),
        provenance=DatasetProvenance(meta["provenance"]),
        coverage_start=from_ns(meta["coverage_start_ns"]),
        coverage_end=from_ns(meta["coverage_end_ns"]),
    )
    for reason in rej["reason"]:
        RejectReason(reason)
    return Dataset(
        ref,
        columns,
        np.array(rej["open_time_ns"], dtype=np.int64),
        np.array(rej["available_at_ns"], dtype=np.int64),
        tuple(rej["reason"]),
        manifest,
    )
