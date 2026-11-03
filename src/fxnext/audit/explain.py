"""``explain``: why did (or didn't) a strategy produce a candidate at a given instant."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from fxnext.audit.store import read_manifest
from fxnext.data.snapshot import SnapshotBuilder
from fxnext.data.store import load_dataset
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.enums import SlotStatus, Timeframe
from fxnext.domain.timeutil import from_ns, to_ns


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("f:"):
            return float.fromhex(value[2:])
        if value.startswith("t:"):
            return from_ns(int(value[2:])).isoformat()
        return value
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    return value


def explain(
    run_dir: Path, symbol: str, at: datetime, datasets_root: Path | None = None
) -> dict[str, Any]:
    manifest = read_manifest(run_dir)
    files = [str(Path(run_dir) / "segments" / s["file"]) for s in manifest["segments"]]
    at_ns = to_ns(at)
    if not FxCalendar().is_in_session(at):
        return {
            "symbol": symbol,
            "at": at.isoformat(),
            "answer": "MARKET_CLOSED_BY_CALENDAR",
            "detail": "instant outside the FX session (weekend or expected closure): "
            "no evaluation is scheduled",
        }
    if not files:
        return {"symbol": symbol, "at": at.isoformat(), "answer": "NO_COMMITTED_EVALUATIONS"}
    con = duckdb.connect()
    row = con.execute(
        "SELECT evaluation_json FROM read_parquet(?) WHERE symbol = ? AND as_of_ns <= ? "
        "ORDER BY as_of_ns DESC LIMIT 1",
        [files, symbol, at_ns],
    ).fetchone()
    if row is None:
        return {"symbol": symbol, "at": at.isoformat(), "answer": "NO_EVALUATION_BEFORE_INSTANT"}
    evaluation = _decode(json.loads(row[0]))
    out: dict[str, Any] = {
        "symbol": symbol,
        "at": at.isoformat(),
        "evaluation_as_of": evaluation["request"]["as_of"],
        "evaluation": evaluation,
    }
    outcome = evaluation["outcome"]
    out["answer"] = "CANDIDATE" if "candidate_id" in outcome else outcome["reason_code"]
    if datasets_root is not None and outcome.get("reason_code") in (
        "BAR_NOT_AVAILABLE",
        "DATA_GAP",
    ):
        out["ex_post"] = _ex_post(manifest, symbol, evaluation, Path(datasets_root))
    return out


def _ex_post(
    manifest: dict[str, Any], symbol: str, evaluation: dict[str, Any], root: Path
) -> dict[str, Any]:
    """Ex-post cause of the first unavailable slot. Never part of any hash."""

    as_of_ns = to_ns(datetime.fromisoformat(evaluation["request"]["as_of"]))
    tf = Timeframe(evaluation["request"]["signal_timeframe"])
    if evaluation["outcome"]["reason_code"] == "BAR_NOT_AVAILABLE":
        slot_open = to_ns(datetime.fromisoformat(evaluation["request"]["expected_close"])) - tf.ns
    else:
        gate = next(f for f in evaluation["filters"] if f["name"] == "window_available")
        slot_open = to_ns(datetime.fromisoformat(gate["measure"]["first_open"]))
    for dataset_id in manifest["fingerprint_inputs"]["dataset_ids"]:
        ds = load_dataset(root, dataset_id)
        if ds.ref.symbol != symbol:
            continue
        builder = SnapshotBuilder(ds, manifest["fingerprint_inputs"]["publication_delay_ns"])
        causes = []
        for k in range(tf.ns // Timeframe.M15.ns):
            m15 = slot_open + k * Timeframe.M15.ns
            status, _ = builder.m15_state(m15, as_of_ns)
            if status is not SlotStatus.UNAVAILABLE:
                continue
            final, idx = builder.m15_state(m15, 2**62)
            if final in (SlotStatus.VALID, SlotStatus.FLAT_ZERO_VOLUME) and idx is not None:
                cause = {
                    "cause": "LATE_ARRIVAL",
                    "available_at": from_ns(int(builder._eff_avail[idx])).isoformat(),
                }
            elif final is SlotStatus.INVALID:
                cause = {"cause": "INVALID_ROW"}
            else:
                cause = {"cause": "MISSING"}
            causes.append({"m15_open": from_ns(m15).isoformat(), **cause})
        return {"ex_post": True, "slot_open": from_ns(slot_open).isoformat(), "m15": causes}
    return {"ex_post": True, "detail": "dataset not found"}
