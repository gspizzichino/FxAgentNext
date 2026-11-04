"""Calendar-driven deterministic replay with crash-safe resume (ADR-0011, ADR-0014)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.audit.store import AuditDestination, CrashHook
from fxnext.data.snapshot import SnapshotBuilder
from fxnext.data.store import Dataset
from fxnext.domain.calendar import CALENDAR_VERSION, FxCalendar
from fxnext.domain.contracts import EMPTY_STATE, StrategyConfig
from fxnext.domain.enums import Dispatch, Timeframe
from fxnext.domain.hashing import HASH_SCHEMA, content_hash
from fxnext.domain.timeutil import from_ns, to_ns
from fxnext.strategy.base import ENGINE_VERSION, SCHEDULER_VERSION
from fxnext.strategy.donchian import DonchianBreakout


class ReplayInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_fingerprint: str
    output_hash: str
    evaluations: int
    path: Path


def classify_dispatch(
    as_of: datetime, executed_at: datetime | None, max_lateness: timedelta
) -> Dispatch:
    """Pure dispatch rule for the runtime (T2.4); replay always passes executed_at=None."""

    if executed_at is None:
        return Dispatch.REPLAY
    if executed_at - as_of > max_lateness:
        return Dispatch.NOT_DISPATCHED_LATE
    return Dispatch.DISPATCHED


def fingerprint_inputs(
    datasets: list[Dataset],
    config: StrategyConfig,
    start: datetime,
    end: datetime,
    evaluation_delay: timedelta,
    publication_delay: timedelta,
    code_version: str,
) -> dict[str, Any]:
    provenances = {d.ref.provenance for d in datasets}
    if len(provenances) != 1:
        raise ReplayInputError("REAL and SYNTHETIC datasets cannot be mixed in one run")
    symbols = [d.ref.symbol for d in datasets]
    if len(set(symbols)) != len(symbols):
        raise ReplayInputError("one dataset per symbol")
    return {
        "dataset_ids": sorted(d.ref.dataset_id for d in datasets),
        "provenance": next(iter(provenances)).value,
        "config_hash": config.config_hash,
        "engine_version": ENGINE_VERSION,
        "scheduler_version": SCHEDULER_VERSION,
        "calendar_version": CALENDAR_VERSION,
        "hash_schema": HASH_SCHEMA,
        "evaluation_delay_ns": int(evaluation_delay.total_seconds() * 1e9),
        "publication_delay_ns": int(publication_delay.total_seconds() * 1e9),
        "symbols": sorted(symbols),
        "start_ns": to_ns(start),
        "end_ns": to_ns(end),
        "code_version": code_version,
    }


Schedule = list[tuple[int, str, int]]  # (as_of_ns, symbol, grid index)


def build_schedule(
    tf: Timeframe,
    start: datetime,
    end: datetime,
    evaluation_delay_ns: int,
    symbols: list[str],
    slots_back: int,
) -> tuple[npt.NDArray[np.int64], Schedule]:
    """Calendar-driven schedule shared by replay and the runtime-like loop (T11)."""

    grid = FxCalendar().slot_grid(tf, start - timedelta(days=60), end + timedelta(days=14))
    first = int(np.searchsorted(grid, to_ns(start)))
    if first < slots_back:
        raise ReplayInputError("calendar padding too small")
    schedule: Schedule = []
    end_ns = to_ns(end)
    for idx in range(first, grid.size):
        if int(grid[idx]) + tf.ns > end_ns:
            break
        for symbol in symbols:
            schedule.append((int(grid[idx]) + tf.ns + evaluation_delay_ns, symbol, idx))
    schedule.sort()
    return grid, schedule


def window_and_next(
    grid: npt.NDArray[np.int64], idx: int, slots_back: int, tf: Timeframe
) -> tuple[npt.NDArray[np.int64], int | None]:
    close_ns = int(grid[idx]) + tf.ns
    nxt = int(grid[idx + 1]) if idx + 1 < grid.size and int(grid[idx + 1]) == close_ns else None
    return grid[idx - slots_back : idx + 1], nxt


def run_replay(
    datasets: list[Dataset],
    runs_root: Path,
    start: datetime,
    end: datetime,
    *,
    config: StrategyConfig | None = None,
    evaluation_delay: timedelta = timedelta(0),
    publication_delay: timedelta = timedelta(0),
    code_version: str,
    crash_hook: CrashHook = lambda point: None,
    stop_after: int | None = None,
) -> ReplayResult | None:
    """Run or resume a replay. Returns None if stopped early via ``stop_after`` (tests)."""

    if code_version.endswith("-dirty"):
        raise ReplayInputError("working tree is dirty: run would be non-reproducible")
    strategy = DonchianBreakout()
    config = config or strategy.default_config()
    req = strategy.requirements(config)[0]
    inputs = fingerprint_inputs(
        datasets, config, start, end, evaluation_delay, publication_delay, code_version
    )
    fingerprint = content_hash("RunFingerprint/1", inputs)
    tf = req.timeframe
    builders = {d.ref.symbol: SnapshotBuilder(d, inputs["publication_delay_ns"]) for d in datasets}
    grid, schedule = build_schedule(
        tf, start, end, inputs["evaluation_delay_ns"], sorted(builders), req.slots_back
    )

    dest = AuditDestination(Path(runs_root), fingerprint, inputs, crash_hook=crash_hook)
    with dest:
        if dest.completed:
            return ReplayResult(
                fingerprint,
                dest.manifest["output_hash"],
                dest.manifest["evaluation_count"],
                dest.path,
            )
        last = dest.last_committed_as_of_ns
        current_month: tuple[int, int] | None = None
        done = 0
        for as_of_ns, symbol, idx in schedule:
            if last is not None and as_of_ns <= last:
                continue
            month = (from_ns(as_of_ns).year, from_ns(as_of_ns).month)
            if current_month is not None and month != current_month:
                dest.commit_segment()
            current_month = month
            window, nxt = window_and_next(grid, idx, req.slots_back, tf)
            snapshot = builders[symbol].build(tf, window, nxt, as_of_ns)
            evaluation = strategy.evaluate(snapshot, config, EMPTY_STATE)
            dest.append(evaluation)
            done += 1
            if stop_after is not None and done >= stop_after:
                return None
        output_hash = dest.complete()
        return ReplayResult(fingerprint, output_hash, dest.manifest["evaluation_count"], dest.path)
