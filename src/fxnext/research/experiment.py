"""Run a pre-registered experiment end to end (M2 spec §5-§9, §13).

replay (M1 engine, audited) -> candidates -> simulated streams -> benchmarks
-> metrics -> criteria -> report. The result directory is written atomically
(tmp + ``os.replace``) under its fingerprint; a crash leaves nothing half
written and the experiment is simply recomputed.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from fxnext.audit.store import iter_rows
from fxnext.data.path import PricePath, load_path, truncate_path
from fxnext.data.store import Dataset, load_dataset
from fxnext.domain.enums import DatasetProvenance, Direction, PriceBasis, Timeframe
from fxnext.domain.hashing import content_hash
from fxnext.domain.timeutil import from_ns, to_ns
from fxnext.replay.runner import ReplayInputError, run_replay
from fxnext.research import benchmarks as bm
from fxnext.research.execution import (
    COST_MODEL_VERSION,
    EXEC_MODEL_VERSION,
    ExitReason,
    Fill,
    Simulator,
    Stream,
    TradeSpec,
    apply_position_policy,
)
from fxnext.research.metrics import Summary, by_year, percentile_of, summarize
from fxnext.research.protocol import (
    PROTOCOL_VERSION,
    HoldoutLog,
    Preregistration,
    ProtocolError,
    load_preregistration,
    split_bounds,
)
from fxnext.strategy.donchian import DonchianBreakout, DonchianParams

RESEARCH_VERSION = "research-v1"
_H1 = Timeframe.H1.ns


def decode_canonical(value: Any) -> Any:
    """Inverse of the canonical float/datetime encoding used in audit rows."""

    if isinstance(value, str):
        if value.startswith("f:"):
            return float.fromhex(value[2:])
        if value.startswith("t:"):
            return int(value[2:])
        return value
    if isinstance(value, list):
        return [decode_canonical(v) for v in value]
    if isinstance(value, dict):
        return {k: decode_canonical(v) for k, v in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class Evaluations:
    candidates: list[TradeSpec]
    points: list[bm.EntryPoint]
    reasons: Counter[str]
    count: int


def read_evaluations(run_dir: Path, symbol: str, params: DonchianParams) -> Evaluations:
    candidates: list[TradeSpec] = []
    points: list[bm.EntryPoint] = []
    reasons: Counter[str] = Counter()
    count = 0
    for row in iter_rows(run_dir):
        if row["symbol"] != symbol:
            continue
        count += 1
        reasons[row["reason_code"] or "CANDIDATE"] += 1
        if row["reason_code"] not in (None, "NO_EVENT"):
            continue
        ev = decode_canonical(json.loads(row["evaluation_json"]))
        close_ns = int(ev["request"]["expected_close"])
        measures = ev["measures"]
        as_of = int(row["as_of_ns"])
        month = from_ns(as_of).year * 12 + from_ns(as_of).month - 1
        points.append(
            bm.EntryPoint(
                symbol, as_of, close_ns + _H1, measures["close_t"], measures["atr"], month
            )
        )
        if row["outcome"] == "CANDIDATE":
            out = ev["outcome"]
            candidates.append(
                TradeSpec(
                    out["candidate_id"],
                    symbol,
                    Direction(out["direction"]),
                    out["entry_ref"],
                    out["stop"],
                    out["target"],
                    int(out["available_at"]),
                    int(out["expires_at"]),
                )
            )
    if params.basis is not PriceBasis.BID:
        raise ValueError("research-v1 expects BID levels")
    return Evaluations(candidates, points, reasons, count)


def benchmark_spec(p: bm.EntryPoint, direction: Direction, params: DonchianParams) -> TradeSpec:
    d = params.stop_atr_mult * p.atr
    sign = 1.0 if direction is Direction.LONG else -1.0
    return TradeSpec(
        f"bench:{p.symbol}:{p.as_of_ns}:{direction.value}",
        p.symbol,
        direction,
        p.close,
        p.close - sign * d,
        p.close + sign * params.target_r * d,
        p.as_of_ns,
        p.expires_at_ns,
    )


def outcome_id(fill: Fill, symbol: str, exec_hash: str, cost_hash: str, path_id: str) -> str:
    return content_hash(
        "SimulatedOutcome/1",
        {
            "key": fill.key,
            "symbol": symbol,
            "stream": fill.stream.value,
            "exec_model_version": EXEC_MODEL_VERSION,
            "exec_params_hash": exec_hash,
            "cost_model_hash": cost_hash,
            "path_id": path_id,
        },
    )


def outcome_rows(
    fills: list[Fill],
    symbol: str,
    direction: dict[str, str],
    variant: str,
    exec_hash: str,
    cost_hash: str,
    path_id: str,
) -> list[dict[str, Any]]:
    rows = []
    for f in fills:
        row = asdict(f)
        row["stream"] = f.stream.value
        row["reason"] = f.reason.value
        row.update(
            outcome_id=outcome_id(f, symbol, exec_hash, cost_hash, path_id),
            symbol=symbol,
            direction=direction.get(f.key),
            variant=variant,
            provenance="MODELLED",
        )
        rows.append(row)
    return rows


def _summary(fills: list[Fill], months: float, prereg: Preregistration) -> Summary:
    counted = [f for f in fills if f.counted]
    return summarize(
        np.array([f.r_net for f in counted], dtype=np.float64),
        np.array([f.entry_ns for f in counted], dtype=np.int64),
        np.array([f.exit_ns for f in counted], dtype=np.int64),
        months=months,
        bootstrap_samples=prereg.bootstrap_samples,
        seed=prereg.bootstrap_seed,
    )


def _months(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / (365.25 / 12 * 86_400)


def evaluate_criteria(
    prereg: Preregistration,
    base: Summary,
    stressed: Summary,
    yearly: dict[int, float],
    pct_random_direction: float,
    pct_random_time: float,
) -> dict[str, Any]:
    c = prereg.criteria
    if prereg.split == "development":
        return {"outcome": "NOT_APPLICABLE", "note": "development split: no decision is taken"}
    exp = base.expectancy
    checks: dict[str, bool] = {}
    if prereg.split == "validation":
        checks = {
            "expectancy_positive": exp is not None and exp > 0,
            "ci_low_positive": base.ci_low is not None and base.ci_low > 0,
            "min_independent_trades": base.independent >= c.min_independent_trades,
            "stressed_expectancy_positive": stressed.expectancy is not None
            and stressed.expectancy > 0,
            "beats_random_direction": pct_random_direction >= c.benchmark_percentile,
            "beats_random_time": pct_random_time >= c.benchmark_percentile,
            "no_year_below_limit": all(v >= c.worst_year_r for v in yearly.values()),
        }
        if all(checks.values()):
            outcome = "VALIDATION_PASSED"  # CANDIDATE_FOR_DEMO still needs the holdout
        elif (
            exp is not None and exp <= 0 and base.ci_high is not None and base.ci_high < 0
        ) or not (checks["beats_random_direction"] and checks["beats_random_time"]):
            outcome = "REJECTED"
        else:
            outcome = "INCONCLUSIVE"
    else:
        ref = prereg.validation_max_drawdown_r
        if ref is None:
            raise ProtocolError("holdout criteria need validation_max_drawdown_r")
        checks = {
            "expectancy_positive": exp is not None and exp > 0,
            "drawdown_within_ratio": base.max_drawdown_r <= c.holdout_max_drawdown_ratio * ref,
        }
        if all(checks.values()):
            outcome = "CANDIDATE_FOR_DEMO"
        elif exp is not None and exp <= 0 and base.ci_high is not None and base.ci_high < 0:
            outcome = "REJECTED"
        else:
            outcome = "INCONCLUSIVE"
    return {"outcome": outcome, "checks": checks}


def reconcile(
    cands: list[TradeSpec],
    signal: list[Fill],
    exec_all: list[Fill],
    executed: list[Fill],
    blocked: list[Fill],
    start_ns: int,
    end_ns: int,
) -> dict[str, Any]:
    """Every candidate has exactly one fate per stream, and nothing crosses the split.

    exec ×1: executed + counterfactual + not entered == candidates (M2 spec §14).
    Raises ``ProtocolError`` on any mismatch: a report that does not add up is not written.
    """

    keys = [c.key for c in cands]
    ex_keys = {f.key for f in executed}
    cf_keys = {f.key for f in blocked}
    not_entered = [f for f in exec_all if f.entry_ns is None]
    every = signal + exec_all
    out: dict[str, Any] = {
        "candidates": len(cands),
        "unique_keys": len(set(keys)),
        "signal_fills": len(signal),
        "signal_counted": sum(f.counted for f in signal),
        "signal_reasons": dict(sorted(Counter(f.reason.value for f in signal).items())),
        "exec_executed": len(executed),
        "exec_executed_counted": sum(f.counted for f in executed),
        "exec_counterfactual": len(blocked),
        "exec_not_entered": len(not_entered),
        "exec_end_of_data": sum(f.reason is ExitReason.END_OF_DATA for f in executed),
        "candidates_available_before_start": sum(c.available_at_ns < start_ns for c in cands),
        "candidates_available_after_end": sum(c.available_at_ns > end_ns for c in cands),
        "candidates_expiring_after_end": sum(c.expires_at_ns > end_ns for c in cands),
        "entries_outside_split": sum(
            f.entry_ns is not None and not start_ns <= f.entry_ns < end_ns for f in every
        ),
        "exits_outside_split": sum(
            f.exit_ns is not None and not start_ns <= f.exit_ns < end_ns for f in every
        ),
    }
    problems = []
    if out["unique_keys"] != len(cands):
        problems.append("duplicate candidate keys")
    if [f.key for f in signal] != keys or [f.key for f in exec_all] != keys:
        problems.append("streams do not cover the candidates one to one")
    if ex_keys & cf_keys:
        problems.append("a candidate is both executed and counterfactual")
    if len(executed) + len(blocked) + len(not_entered) != len(cands):
        problems.append("executed + counterfactual + not entered != candidates")
    for k in (
        "candidates_available_before_start",
        "candidates_available_after_end",
        "entries_outside_split",
        "exits_outside_split",
    ):
        if out[k]:
            problems.append(k)
    out["balanced"] = not problems
    if problems:
        raise ProtocolError("reconciliation failed: " + "; ".join(problems))
    return out


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    fingerprint: str
    path: Path
    report: dict[str, Any]


def run_experiment(
    prereg_path: Path,
    *,
    datasets_root: Path,
    paths_root: Path,
    runs_root: Path,
    research_root: Path,
    code_version: str,
    reopen_holdout: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ExperimentResult:
    prereg, prereg_sha = load_preregistration(prereg_path)
    if code_version.endswith("-dirty"):
        raise ReplayInputError("working tree is dirty: experiment would be non-reproducible")
    strategy = DonchianBreakout()
    if (prereg.strategy_id, prereg.strategy_version) != (
        strategy.strategy_id,
        strategy.strategy_version,
    ):
        raise ProtocolError("research-v1 supports only baseline_donchian_breakout v1")
    config = strategy.default_config()
    params = config.params
    assert isinstance(params, DonchianParams)
    start, end = split_bounds(prereg.split)
    datasets: list[Dataset] = []
    paths: dict[str, PricePath] = {}
    for item in prereg.data:
        ds = load_dataset(datasets_root, item.m15_bid_dataset)
        path = load_path(paths_root, item.path)
        if ds.ref.symbol != item.symbol or path.symbol != item.symbol:
            raise ProtocolError(f"data for {item.symbol} does not match its symbol")
        if ds.ref.basis is not PriceBasis.BID:
            raise ProtocolError("the strategy dataset must be BID")
        if ds.ref.provenance is not path.provenance:
            raise ProtocolError("dataset and path provenance differ")
        datasets.append(ds)
        paths[item.symbol] = path
    end = min(end, *(d.ref.coverage_end for d in datasets))
    if end <= start:
        raise ProtocolError("data does not cover the split")
    provenance = datasets[0].ref.provenance
    # Exits are resolved only with prices of this split (M2 spec §14, split boundary).
    source_paths = {s: p.path_id for s, p in sorted(paths.items())}
    paths = {s: truncate_path(p, to_ns(end)) for s, p in paths.items()}

    exec_hash = content_hash("ExecParams/1", prereg.execution)
    cost_hash = content_hash("CostModel/1", prereg.costs)
    if prereg.split == "holdout":
        HoldoutLog(research_root).open(prereg, prereg_sha, reopen=reopen_holdout, now=now())

    replay = run_replay(datasets, runs_root, start, end, config=config, code_version=code_version)
    assert replay is not None
    fingerprint = content_hash(
        "ResearchFingerprint/1",
        {
            "research_version": RESEARCH_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "prereg_sha256": prereg_sha,
            "run_fingerprint": replay.run_fingerprint,
            "output_hash": replay.output_hash,
            "path_ids": sorted(p.path_id for p in paths.values()),
            "exec_params_hash": exec_hash,
            "cost_model_hash": cost_hash,
            "effective_end": end,
            "code_version": code_version,
        },
    )
    final = Path(research_root) / fingerprint
    if (final / "report.json").exists():
        return ExperimentResult(fingerprint, final, json.loads((final / "report.json").read_text()))

    months = _months(start, end)
    rows: list[dict[str, Any]] = []
    per_symbol: dict[str, Any] = {}
    all_fills: dict[str, list[Fill]] = {"signal": [], "counterfactual": []}
    exec_by_mult: dict[float, list[Fill]] = {m: [] for m in prereg.cost_multipliers}
    exec_target_first: list[Fill] = []
    pooled_points: list[bm.EntryPoint] = []
    pooled_pairs: list[tuple[Fill, Fill]] = []
    cand_points: list[bm.EntryPoint] = []
    cand_pairs: list[tuple[Fill, Fill]] = []
    entries_per_month: Counter[int] = Counter()
    coverage: dict[str, Any] = {}
    reconciliation: dict[str, Any] = {}
    target_first = prereg.execution.model_copy(update={"ambiguity": "TARGET_FIRST"})

    for symbol in sorted(paths):
        sim = Simulator(paths[symbol])
        evs = read_evaluations(replay.path, symbol, params)
        cands = sorted(evs.candidates, key=lambda s: (s.available_at_ns, s.key))
        direction = {c.key: c.direction.value for c in cands}
        signal = [sim.simulate(c, Stream.SIGNAL, prereg.execution, prereg.costs) for c in cands]
        sym_exec: dict[float, list[Fill]] = {}
        for m in prereg.cost_multipliers:
            fills = [
                sim.simulate(c, Stream.EXEC, prereg.execution, prereg.costs.scaled(m))
                for c in cands
            ]
            executed, blocked = apply_position_policy(fills)
            if m == 1.0:
                base_all, base_blocked = fills, [fills[i] for i in blocked]
            sym_exec[m] = [fills[i] for i in executed]
            exec_by_mult[m].extend(sym_exec[m])
            if m == 1.0:
                cf = [
                    Fill(**{**asdict(fills[i]), "stream": Stream.COUNTERFACTUAL}) for i in blocked
                ]
                all_fills["counterfactual"].extend(cf)
                rows += outcome_rows(
                    cf, symbol, direction, "x1", exec_hash, cost_hash, paths[symbol].path_id
                )
            rows += outcome_rows(
                sym_exec[m],
                symbol,
                direction,
                f"x{m:g}",
                exec_hash,
                cost_hash,
                paths[symbol].path_id,
            )
        tf_fills = [sim.simulate(c, Stream.EXEC, target_first, prereg.costs) for c in cands]
        exec_target_first.extend(tf_fills[i] for i in apply_position_policy(tf_fills)[0])
        all_fills["signal"].extend(signal)
        rows += outcome_rows(
            signal, symbol, direction, "x1", exec_hash, cost_hash, paths[symbol].path_id
        )

        # benchmarks: both directions at every eligible instant, simulated once
        by_as_of = {p.as_of_ns: p for p in evs.points}
        for c in cands:
            p = by_as_of[c.available_at_ns]
            cand_points.append(p)
            cand_pairs.append(
                tuple(  # type: ignore[arg-type]
                    sim.simulate(
                        benchmark_spec(p, d, params), Stream.EXEC, prereg.execution, prereg.costs
                    )
                    for d in (Direction.LONG, Direction.SHORT)
                )
            )
            entries_per_month[p.month] += 1
        for p in evs.points:
            pooled_points.append(p)
            pooled_pairs.append(
                tuple(  # type: ignore[arg-type]
                    sim.simulate(
                        benchmark_spec(p, d, params), Stream.EXEC, prereg.execution, prereg.costs
                    )
                    for d in (Direction.LONG, Direction.SHORT)
                )
            )
        base_fills = sym_exec[1.0] if 1.0 in sym_exec else next(iter(sym_exec.values()))
        if 1.0 not in sym_exec:
            raise ProtocolError("cost_multipliers must include 1.0")
        reconciliation[symbol] = reconcile(
            cands, signal, base_all, base_fills, base_blocked, to_ns(start), to_ns(end)
        )
        per_symbol[symbol] = {
            "exec_x1": _summary(base_fills, months, prereg).as_dict(),
            "signal": _summary(signal, months, prereg).as_dict(),
            "by_year_r": by_year(
                np.array([f.r_net for f in base_fills if f.counted], dtype=np.float64),
                np.array([f.entry_ns for f in base_fills if f.counted], dtype=np.int64),
            ),
        }
        coverage[symbol] = {
            "evaluations": evs.count,
            "by_reason": dict(sorted(evs.reasons.items())),
            "candidates": len(cands),
            "exec_reasons": dict(sorted(Counter(f.reason.value for f in base_fills).items())),
            "not_filled": sum(f.reason is ExitReason.NOT_FILLED_NO_QUOTE for f in signal),
            "exec_not_entered": sum(f.entry_ns is None for f in base_all),
            "end_of_data": sum(f.reason is ExitReason.END_OF_DATA for f in base_fills),
            "ambiguous": sum(f.ambiguous for f in base_fills),
            "path_gap": sum(f.path_gap for f in base_fills),
            "weekend_crossings": sum(f.weekend_crossings for f in base_fills),
            "path_quality": paths[symbol].quality,
        }

    base_list = exec_by_mult.get(1.0, [])
    base = _summary(base_list, months, prereg)
    stressed_m = prereg.criteria.cost_stress_multiplier
    stressed = _summary(exec_by_mult.get(stressed_m, base_list), months, prereg)
    yearly = by_year(
        np.array([f.r_net for f in base_list if f.counted], dtype=np.float64),
        np.array([f.entry_ns for f in base_list if f.counted], dtype=np.int64),
    )
    cand_paired = bm.PairedFills.of(cand_points, cand_pairs)
    pool_paired = bm.PairedFills.of(pooled_points, pooled_pairs)
    rd = bm.random_direction(cand_paired, prereg.benchmark_repetitions, prereg.benchmark_seed)
    rt = bm.random_time(
        pool_paired,
        dict(entries_per_month),
        prereg.benchmark_repetitions,
        prereg.benchmark_seed + 1,
    )
    exp = base.expectancy if base.expectancy is not None else float("nan")
    rd_ok, rt_ok = rd[~np.isnan(rd)], rt[~np.isnan(rt)]
    pct_rd = percentile_of(exp, rd_ok)
    pct_rt = percentile_of(exp, rt_ok)
    benchmarks = {
        "random_direction": _dist(rd_ok, pct_rd),
        "random_time": _dist(rt_ok, pct_rt),
        "always_long": bm.fixed_direction(cand_paired, Direction.LONG),
        "always_short": bm.fixed_direction(cand_paired, Direction.SHORT),
    }
    criteria = evaluate_criteria(prereg, base, stressed, yearly, pct_rd, pct_rt)
    report: dict[str, Any] = {
        "fingerprint": fingerprint,
        "experiment_id": prereg.experiment_id,
        "hypothesis": prereg.hypothesis,
        "split": prereg.split,
        "interval": {"start": start.isoformat(), "end": end.isoformat()},
        "provenance": provenance.value,
        "prereg_sha256": prereg_sha,
        "strategy": {
            "strategy_id": config.strategy_id,
            "strategy_version": config.strategy_version,
            "params": params.model_dump(mode="json"),
        },
        "versions": {
            "research": RESEARCH_VERSION,
            "protocol": PROTOCOL_VERSION,
            "exec_model": EXEC_MODEL_VERSION,
            "cost_model": COST_MODEL_VERSION,
            "code_version": code_version,
        },
        "inputs": {
            "run_fingerprint": replay.run_fingerprint,
            "output_hash": replay.output_hash,
            "datasets": sorted(d.ref.dataset_id for d in datasets),
            "paths": {s: p.path_id for s, p in sorted(paths.items())},
            "source_paths": source_paths,
            "exec_params": prereg.execution.model_dump(mode="json"),
            "costs": prereg.costs.model_dump(mode="json"),
        },
        "streams": {
            "exec": {
                f"x{m:g}": _summary(v, months, prereg).as_dict() for m, v in exec_by_mult.items()
            },
            "exec_target_first": _summary(exec_target_first, months, prereg).as_dict(),
            "signal": _summary(all_fills["signal"], months, prereg).as_dict(),
            "counterfactual": _summary(all_fills["counterfactual"], months, prereg).as_dict(),
            "broker": None,
        },
        "by_year_r": yearly,
        "per_symbol": per_symbol,
        "benchmarks": benchmarks,
        "coverage": coverage,
        "reconciliation": reconciliation,
        "criteria": criteria,
        "warnings": _warnings(provenance, paths, end, split_bounds(prereg.split)[1]),
    }
    _write_atomic(Path(research_root), fingerprint, report, rows)
    return ExperimentResult(fingerprint, final, report)


GRID_CHANNEL = (10, 20, 40)
GRID_STOP_ATR = (1.0, 1.5, 2.0)
GRID_TARGET_R = (1.5, 2.0, 3.0)


def run_grid(
    prereg_path: Path,
    *,
    datasets_root: Path,
    paths_root: Path,
    runs_root: Path,
    code_version: str,
) -> list[dict[str, Any]]:
    """Informative parameter grid (M2 spec §7). Development split only, selects nothing."""

    from fxnext.domain.contracts import StrategyConfig
    from fxnext.research.protocol import check_development_only

    prereg, _ = load_preregistration(prereg_path)
    start, end = split_bounds(prereg.split)
    check_development_only(start, end)
    strategy = DonchianBreakout()
    base = strategy.default_config()
    datasets = [load_dataset(datasets_root, d.m15_bid_dataset) for d in prereg.data]
    paths = {d.symbol: load_path(paths_root, d.path) for d in prereg.data}
    end = min(end, *(d.ref.coverage_end for d in datasets))
    months = _months(start, end)
    out = []
    for n in GRID_CHANNEL:
        for k in GRID_STOP_ATR:
            for t in GRID_TARGET_R:
                params = DonchianParams(channel_slots=n, stop_atr_mult=k, target_r=t)
                config = StrategyConfig(
                    strategy_id=base.strategy_id,
                    strategy_version=base.strategy_version,
                    params=params,
                    detection_param_names=base.detection_param_names,
                )
                replay = run_replay(
                    datasets, runs_root, start, end, config=config, code_version=code_version
                )
                assert replay is not None
                fills: list[Fill] = []
                for symbol, path in sorted(paths.items()):
                    sim = Simulator(path)
                    evs = read_evaluations(replay.path, symbol, params)
                    cands = sorted(evs.candidates, key=lambda c: (c.available_at_ns, c.key))
                    sim_fills = [
                        sim.simulate(c, Stream.EXEC, prereg.execution, prereg.costs) for c in cands
                    ]
                    fills += [sim_fills[i] for i in apply_position_policy(sim_fills)[0]]
                out.append(
                    {
                        "channel_slots": n,
                        "stop_atr_mult": k,
                        "target_r": t,
                        **_summary(fills, months, prereg).as_dict(),
                    }
                )
    return out


def _dist(values: np.ndarray, pct: float) -> dict[str, Any]:
    if values.size == 0:
        return {"repetitions": 0}
    q = np.quantile(values, [0.05, 0.5, 0.95])
    return {
        "repetitions": int(values.size),
        "p05": float(q[0]),
        "median": float(q[1]),
        "p95": float(q[2]),
        "baseline_percentile": pct,
    }


def _warnings(
    provenance: DatasetProvenance, paths: dict[str, PricePath], end: datetime, split_end: datetime
) -> list[str]:
    out = []
    if provenance is DatasetProvenance.SYNTHETIC:
        out.append("SYNTHETIC data: results say nothing about real markets")
    if end < split_end:
        out.append(f"data ends at {end.isoformat()}, before the end of the split")
    if set(paths) != {"EURUSD", "GBPUSD", "USDJPY"}:
        out.append(f"symbols evaluated: {', '.join(sorted(paths))}")
    return out


def _write_atomic(
    root: Path, fingerprint: str, report: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    from fxnext.research.report import render_markdown

    root.mkdir(parents=True, exist_ok=True)
    final = root / fingerprint
    tmp = root / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        pq.write_table(
            pa.Table.from_pylist(rows) if rows else pa.table({}), tmp / "outcomes.parquet"
        )
        (tmp / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
        (tmp / "report.md").write_text(render_markdown(report), encoding="utf-8")
        for name in ("outcomes.parquet", "report.json", "report.md"):
            with (tmp / name).open("ab") as handle:
                os.fsync(handle.fileno())
        if final.exists():
            shutil.rmtree(tmp)
            return
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


__all__ = ["ExperimentResult", "run_experiment", "to_ns"]
