"""Run a hypothesis pre-registration on the development split (S0-S3, S7).

path (hash) -> bars -> descriptive phase -> candidates per variant -> simulated
streams (signal, exec x1/x1.5/x2, counterfactual) -> reconciliation ->
benchmarks with max-of-K -> entry test -> threshold -> report.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from fxnext.data.path import PricePath, load_path, truncate_path
from fxnext.domain.enums import Direction
from fxnext.domain.hashing import content_hash
from fxnext.domain.timeutil import to_ns
from fxnext.replay.runner import ReplayInputError
from fxnext.research.execution import (
    COST_MODEL_VERSION,
    EXEC_MODEL_VERSION,
    PIP_SIZE,
    Fill,
    Simulator,
    Stream,
    TradeSpec,
    apply_position_policy,
)
from fxnext.research.experiment import _months, _summary_arrays, outcome_rows, reconcile
from fxnext.research.hyp import bench
from fxnext.research.hyp.desc import s0_symbol, s3_phase1, s7_phase1, session_returns, weekend_rows
from fxnext.research.hyp.prereg import (
    HYP_PROTOCOL_VERSION,
    HypothesisPrereg,
    Variant,
    load_hypothesis,
)
from fxnext.research.hyp.strategies import RULES, Context, DailyTrend, Meta, NeutralKind, Rule
from fxnext.research.hyp.swap import RATES_VERSION, swap_price
from fxnext.research.metrics import percentile_of
from fxnext.research.protocol import ProtocolError, split_bounds

HYP_RESEARCH_VERSION = "hypothesis-research-v1"


@dataclass(frozen=True, slots=True)
class HypothesisResult:
    fingerprint: str
    path: Path
    report: dict[str, Any]


def _load_paths(pre: HypothesisPrereg, paths_root: Path) -> dict[str, PricePath]:
    out: dict[str, PricePath] = {}
    for ref in pre.data:
        p = load_path(paths_root, ref.path)
        if p.symbol != ref.symbol:
            raise ProtocolError(f"path {ref.path[:8]} is not {ref.symbol}")
        out[ref.symbol] = p
    if len({p.provenance for p in out.values()}) != 1:
        raise ProtocolError("paths of different provenance")
    return out


def run_hypothesis(
    prereg_path: Path,
    *,
    paths_root: Path,
    research_root: Path,
    code_version: str,
    progress: Callable[[str], None] = lambda _: None,
    force_phase2: bool = False,
) -> HypothesisResult:
    """``force_phase2`` (tests only, never from the CLI) runs every variant even when
    the descriptive phase would stop the hypothesis; the report says so."""

    pre, sha = load_hypothesis(prereg_path)
    if code_version.endswith("-dirty"):
        raise ReplayInputError("working tree is dirty: experiment would be non-reproducible")
    start, end = split_bounds(pre.split)
    source = _load_paths(pre, paths_root)
    end = min(
        end,
        *(datetime.fromtimestamp(p.coverage_end_ns / 1e9, start.tzinfo) for p in source.values()),
    )
    if end <= start:
        raise ProtocolError("data does not cover the split")
    start_ns, end_ns = to_ns(start), to_ns(end)
    paths = {s: truncate_path(p, end_ns) for s, p in source.items()}
    fingerprint = content_hash(
        "HypothesisFingerprint/1",
        {
            "research_version": HYP_RESEARCH_VERSION,
            "protocol_version": HYP_PROTOCOL_VERSION,
            "prereg_sha256": sha,
            "path_ids": sorted(p.path_id for p in paths.values()),
            "effective_end": end,
            "code_version": code_version,
            "phase2_forced": force_phase2,
        },
    )
    final = Path(research_root) / fingerprint
    if (final / "report.json").exists():
        return HypothesisResult(fingerprint, final, json.loads((final / "report.json").read_text()))

    ctxs = {s: Context(paths[s], start_ns, end_ns) for s in sorted(paths)}
    report: dict[str, Any] = {
        "fingerprint": fingerprint,
        "experiment_id": pre.experiment_id,
        "hypothesis": pre.hypothesis,
        "kind": pre.kind,
        "exploratory": pre.exploratory,
        "split": pre.split,
        "interval": {"start": start.isoformat(), "end": end.isoformat()},
        "provenance": next(iter(paths.values())).provenance.value,
        "prereg_sha256": sha,
        "frozen_text_sha256": pre.frozen_text_sha256,
        "versions": {
            "research": HYP_RESEARCH_VERSION,
            "protocol": HYP_PROTOCOL_VERSION,
            "exec_model": EXEC_MODEL_VERSION,
            "cost_model": COST_MODEL_VERSION,
            "rates": RATES_VERSION if pre.swap else None,
            "code_version": code_version,
        },
        "phase2_forced": force_phase2,
        "inputs": {
            "paths": {s: p.path_id for s, p in sorted(paths.items())},
            "source_paths": {s: p.path_id for s, p in sorted(source.items())},
            "reference_reports": {d.symbol: d.reference_report for d in pre.data},
            "exec_params": pre.execution.model_dump(mode="json"),
            "costs": pre.costs.model_dump(mode="json"),
            "swap": None if pre.swap is None else pre.swap.model_dump(mode="json"),
        },
        "variants_listed": [v.model_dump(mode="json", exclude_none=True) for v in pre.variants],
    }
    rows: list[dict[str, Any]] = []
    extra_tables: dict[str, list[dict[str, Any]]] = {}

    run_variants = list(pre.variants)
    if pre.kind == "s0_variance_ratio":
        progress("S0 variance ratio")
        report["s0"] = {s: s0_symbol(c.path, start_ns, end_ns) for s, c in ctxs.items()}
    elif pre.kind == "s7_intraday":
        progress("S7 fase 1")
        rets, excluded = [], {}
        for s, c in ctxs.items():
            r, ex = session_returns(c.path, start_ns, end_ns)
            rets += r
            excluded[s] = ex
        phase1 = s7_phase1(rets) | {"excluded": excluded}
        report["phase1"] = phase1
        allowed = set(phase1["phase2_variants"])
        run_variants = [v for v in pre.variants if v.name in allowed or force_phase2]
    elif pre.kind == "s3_sunday_gap":
        progress("S3 fase 1")
        ws = [w for c in ctxs.values() for w in c.weekends()]
        phase1 = s3_phase1(ws, pre.phase1_ks)
        report["phase1"] = phase1
        extra_tables["weekends"] = weekend_rows(ws)
        run_variants = [v for v in pre.variants if v.k in phase1["phase2_k"] or force_phase2]

    if pre.kind != "s0_variance_ratio":
        rule = RULES[pre.kind]
        months = _months(start, end)
        variants: dict[str, Any] = {}
        rd_by_variant: dict[str, np.ndarray] = {}
        rt_by_variant: dict[str, np.ndarray] = {}
        for v in run_variants:
            progress(f"variante {v.name}")
            res, vrows, rd, rt = _run_variant(rule, ctxs, v, pre, months, start_ns, end_ns)
            variants[v.name] = res
            rows += vrows
            if rd is not None:
                rd_by_variant[v.name] = rd
            if rt is not None:
                rt_by_variant[v.name] = rt
        report["variants"] = variants
        report["variants_not_run"] = [v.name for v in pre.variants if v not in run_variants]
        report["assessment"] = _assess(pre, variants, rd_by_variant, rt_by_variant)
    report["warnings"] = _warnings(pre, paths, end, split_bounds(pre.split)[1])
    if force_phase2:
        report["warnings"].append("phase 2 forced for testing: not a valid research result")
    _write_atomic(Path(research_root), fingerprint, report, rows, extra_tables)
    return HypothesisResult(fingerprint, final, report)


# Swap per simulated fill (S2): r_net already includes it; the ledger keeps the parts.
SwapLedger = dict[Fill, tuple[float, int]]


def _with_swap(
    fill: Fill, spec: TradeSpec, ctx: Context, pre: HypothesisPrereg, ledger: SwapLedger
) -> Fill:
    if pre.swap is None or not fill.counted:
        return fill
    assert fill.entry_ns is not None and fill.exit_ns is not None and fill.r_net is not None
    assert fill.entry_price is not None
    rule = RULES[pre.kind]
    assert isinstance(rule, DailyTrend)
    days = rule.rollover_days(ctx, fill.entry_ns, fill.exit_ns)
    amount, nights = swap_price(
        ctx.symbol,
        spec.direction is Direction.LONG,
        fill.entry_price,
        days,
        pre.swap.markup_pct_per_year,
    )
    swap_r = amount / spec.risk
    out = replace(fill, r_net=fill.r_net + swap_r)
    ledger[out] = (swap_r, nights)
    return out


def _simulate(
    sim: Simulator,
    specs: list[TradeSpec],
    ctx: Context,
    pre: HypothesisPrereg,
    m: float,
    ledger: SwapLedger,
) -> list[Fill]:
    costs = pre.costs.scaled(m)
    return [
        _with_swap(sim.simulate(s, Stream.EXEC, pre.execution, costs), s, ctx, pre, ledger)
        for s in specs
    ]


def _run_variant(
    rule: Rule,
    ctxs: dict[str, Context],
    v: Variant,
    pre: HypothesisPrereg,
    months: float,
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray | None, np.ndarray | None]:
    per_symbol: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    exec_by_m: dict[float, list[Fill]] = {m: [] for m in pre.cost_multipliers}
    signal_all: list[Fill] = []
    cf_all: list[Fill] = []
    spec_by_key: dict[str, TradeSpec] = {}
    cands_by_symbol: dict[str, list[tuple[TradeSpec, Meta]]] = {}
    sims: dict[str, Simulator] = {}
    ledger: SwapLedger = {}
    exec_hash = content_hash("ExecParams/1", pre.execution)
    cost_hash = content_hash("CostModel/1", pre.costs)
    for sym, ctx in ctxs.items():
        cands = rule.candidates(ctx, v)
        cands_by_symbol[sym] = cands
        specs = [c for c, _ in cands]
        spec_by_key.update({s.key: s for s in specs})
        sim = sims[sym] = Simulator(ctx.path)
        direction = {s.key: s.direction.value for s in specs}
        signal = [sim.simulate(s, Stream.SIGNAL, pre.execution, pre.costs) for s in specs]
        signal_all += signal
        base_all: list[Fill] = []
        base_exec: list[Fill] = []
        base_blocked: list[Fill] = []
        for m in pre.cost_multipliers:
            fills = _simulate(sim, specs, ctx, pre, m, ledger)
            executed, blocked = apply_position_policy(fills)
            ex = [fills[i] for i in executed]
            exec_by_m[m] += ex
            rows += outcome_rows(
                ex, sym, direction, f"{v.name}:x{m:g}", exec_hash, cost_hash, ctx.path.path_id
            )
            if m == 1.0:
                base_all, base_exec = fills, ex
                base_blocked = [fills[i] for i in blocked]
                cf = [replace(f, stream=Stream.COUNTERFACTUAL) for f in base_blocked]
                cf_all += cf
                rows += outcome_rows(
                    cf, sym, direction, f"{v.name}:x1", exec_hash, cost_hash, ctx.path.path_id
                )
        rows += outcome_rows(
            signal, sym, direction, f"{v.name}:x1", exec_hash, cost_hash, ctx.path.path_id
        )
        overlap = getattr(rule, "baseline_overlap", None)
        baseline = overlap(ctx, v, cands) if overlap is not None else None
        per_symbol[sym] = {
            "candidates": len(specs),
            "baseline_reconciliation": baseline,
            "exec_x1": _summary_arrays(base_exec, months, pre).as_dict(),
            "exec_reasons": dict(sorted(Counter(f.reason.value for f in base_exec).items())),
            "reconciliation": reconcile(
                specs, signal, base_all, base_exec, base_blocked, start_ns, end_ns
            ),
        }
    base = exec_by_m[1.0]
    res: dict[str, Any] = {
        "params": v.model_dump(mode="json", exclude_none=True),
        "streams": {
            "exec": {
                f"x{m:g}": _summary_arrays(f, months, pre).as_dict() for m, f in exec_by_m.items()
            },
            "signal": _summary_arrays(signal_all, months, pre).as_dict(),
            "counterfactual": _summary_arrays(cf_all, months, pre).as_dict(),
            "broker": None,
        },
        "per_symbol": per_symbol,
        "units": _units(base, spec_by_key),
    }
    if pre.swap is not None:
        res["swap"] = _swap_stats(base, months, pre, ledger)
        res["same_week_entries"] = _same_week(base)
    # entry test (common rule 8)
    entry_test: dict[str, Any] = {}
    kinds: tuple[NeutralKind, ...] = ("nbar", "sym1r")
    for kind in kinds:
        fills_k: list[Fill] = []
        for sym, cands in cands_by_symbol.items():
            for spec, meta in cands:
                n_spec = rule.neutral(ctxs[sym], v, spec, meta, kind, pre.entry_test_bars)
                if n_spec is not None:
                    fills_k.append(
                        sims[sym].simulate(n_spec, Stream.SIGNAL, pre.execution, pre.costs)
                    )
        if fills_k:
            g = np.array([f.r_gross for f in fills_k if f.counted], dtype=np.float64)
            entry_test[kind] = {
                "trades": int(g.size),
                "gross_win_rate": float((g > 0).mean()) if g.size else None,
                "gross_mean_r": float(g.mean()) if g.size else None,
            }
    res["entry_test"] = entry_test or None
    # benchmarks
    rd = rt = None
    mirrors: list[bench.Paired] = []
    pools: list[bench.Paired] = []
    per_month: list[dict[int, int]] = []
    ratio = _median_ratio(cands_by_symbol)
    for sym, cands in cands_by_symbol.items():
        ctx, sim = ctxs[sym], sims[sym]
        if rule.random_direction:
            pairs = [rule.mirror(ctx, v, s, m) for s, m in cands]
            mirrors.append(_paired(sim, [p for p in pairs if p is not None], ctx, pre, ledger))
        pool = rule.pool(ctx, v, ratio)
        if pool is not None:
            pools.append(_paired(sim, pool, ctx, pre, ledger))
            per_month.append(dict(Counter(bench.month_of(s.available_at_ns) for s, _ in cands)))
    benchmarks: dict[str, Any] = {}
    if mirrors:
        rd = bench.random_direction(mirrors, pre.benchmark_repetitions, pre.benchmark_seed)
        benchmarks["random_direction"] = _dist(rd, res["streams"]["exec"]["x1"]["expectancy"])
        benchmarks["always_long"] = bench.fixed_direction(mirrors, 0)
        benchmarks["always_short"] = bench.fixed_direction(mirrors, 1)
    if pools:
        rt = bench.random_time(pools, per_month, pre.benchmark_repetitions, pre.benchmark_seed + 1)
        benchmarks["random_time"] = _dist(rt, res["streams"]["exec"]["x1"]["expectancy"])
        benchmarks["random_time_stop_atr_ratio"] = ratio
    res["benchmarks"] = benchmarks
    return res, rows, rd, rt


def _median_ratio(cands: dict[str, list[tuple[TradeSpec, Meta]]]) -> float:
    """Median stop distance in ATR of the variant's candidates (geometry only, no outcome)."""

    ratios = [s.risk / m.atr for c in cands.values() for s, m in c if m.atr > 0]
    return float(np.median(ratios)) if ratios else 1.0


def _paired(
    sim: Simulator,
    pairs: list[tuple[TradeSpec, TradeSpec]],
    ctx: Context,
    pre: HypothesisPrereg,
    ledger: SwapLedger,
) -> bench.Paired:
    fills = []
    for long, short in pairs:
        fills.append(
            tuple(
                _with_swap(
                    sim.simulate(s, Stream.EXEC, pre.execution, pre.costs), s, ctx, pre, ledger
                )
                for s in (long, short)
            )
        )
    return bench.Paired.of(fills, [p[0].available_at_ns for p in pairs])  # type: ignore[arg-type]


def _units(fills: list[Fill], specs: dict[str, TradeSpec]) -> dict[str, Any]:
    """Mean cost in R and net expectancy in pips (pre-registration S1, S7)."""

    counted = [f for f in fills if f.counted]
    if not counted:
        return {"trades": 0}
    pips, cost_r = [], []
    for f in counted:
        spec = specs[f.key]
        pip = PIP_SIZE[spec.symbol]
        assert f.r_net is not None
        pips.append(f.r_net * spec.risk / pip)
        cost_r.append((f.spread_pips + f.slippage_pips) * pip / spec.risk + f.commission_r)
    return {
        "trades": len(counted),
        "net_expectancy_pips": float(np.mean(pips)),
        "mean_cost_r": float(np.mean(cost_r)),
        "median_risk_pips": float(
            np.median([specs[f.key].risk / PIP_SIZE[specs[f.key].symbol] for f in counted])
        ),
    }


def _swap_stats(
    fills: list[Fill], months: float, pre: HypothesisPrereg, ledger: SwapLedger
) -> dict[str, Any]:
    counted = [f for f in fills if f.counted]
    parts = [ledger[f] for f in counted]
    no_swap = [
        replace(f, r_net=(f.r_net or 0.0) - p[0]) for f, p in zip(counted, parts, strict=True)
    ]
    return {
        "net_without_swap": _summary_arrays(no_swap, months, pre).as_dict(),
        "mean_swap_r": float(np.mean([p[0] for p in parts])) if parts else None,
        "mean_nights": float(np.mean([p[1] for p in parts])) if parts else None,
        "total_nights": int(sum(p[1] for p in parts)),
    }


def _same_week(fills: list[Fill]) -> dict[str, Any]:
    from fxnext.research.metrics import week_keys

    entries = [(f.key.split(":")[0], f.entry_ns) for f in fills if f.entry_ns is not None]
    weeks: dict[int, set[str]] = {}
    for sym, t in entries:
        wk = int(week_keys(np.array([t], np.int64))[0])
        weeks.setdefault(wk, set()).add(sym)
    multi = [w for w, s in weeks.items() if len(s) > 1]
    return {
        "weeks_with_entries": len(weeks),
        "weeks_with_entries_on_2_or_more_pairs": len(multi),
        "entries_in_those_weeks": sum(
            1 for sym, t in entries if int(week_keys(np.array([t], np.int64))[0]) in set(multi)
        ),
    }


def _dist(values: np.ndarray, expectancy: float | None) -> dict[str, Any]:
    ok = values[~np.isnan(values)]
    if ok.size == 0:
        return {"repetitions": 0}
    q = np.quantile(ok, [0.05, 0.5, 0.95])
    return {
        "repetitions": int(ok.size),
        "p05": float(q[0]),
        "median": float(q[1]),
        "p95": float(q[2]),
        "variant_percentile": float(percentile_of(expectancy, ok))
        if expectancy is not None
        else None,
    }


def _assess(
    pre: HypothesisPrereg,
    variants: dict[str, Any],
    rd: dict[str, np.ndarray],
    rt: dict[str, np.ndarray],
) -> dict[str, Any]:
    """Threshold to propose validation (common rules, frozen 2026-11-11)."""

    th = pre.threshold
    listed = len(pre.variants)
    if not variants:
        return {"outcome": "CLOSED_AT_PHASE1", "note": "nessuna variante supera la fase 1"}
    judged = [pre.main_variant] if pre.main_variant else list(variants)
    judged = [j for j in judged if j in variants]
    if not judged:
        return {
            "outcome": "CLOSED_AT_PHASE1",
            "note": "la variante principale non supera la fase 1",
        }

    def exp(name: str) -> float:
        e = variants[name]["streams"]["exec"]["x1"]["expectancy"]
        return -math.inf if e is None else float(e)

    best = max(judged, key=exp)
    best_exp = exp(best)
    checks: dict[str, bool | None] = {"net_positive_x1": best_exp > 0}
    max_k: dict[str, Any] = {}
    for name, dists in (("random_direction", rd), ("random_time", rt)):
        sel = [dists[j] for j in judged if j in dists]
        if len(sel) == len(judged):
            mk = bench.max_of_k(sel)
            ok = mk[~np.isnan(mk)]
            p = float(np.percentile(ok, th.benchmark_percentile)) if ok.size else math.nan
            max_k[name] = {"k": len(sel), "p95_of_max": p, "repetitions": int(ok.size)}
            checks[f"above_max_{name}_p95"] = bool(best_exp > p)
        else:
            checks[f"above_max_{name}_p95"] = None
    pos_pairs = sum(
        1 for s in variants[best]["per_symbol"].values() if (s["exec_x1"]["expectancy"] or 0.0) > 0
    )
    checks["positive_pairs"] = pos_pairs >= th.min_positive_pairs
    need = math.ceil(th.plateau_positive * listed / th.plateau_of)
    positive = sum(1 for n in variants if exp(n) > 0)
    checks["plateau"] = positive >= need
    entry_rule = None
    if th.entry_test_min_win_rate is not None:
        rates = {
            n: (variants[n]["entry_test"] or {}).get("sym1r", {}).get("gross_win_rate")
            for n in variants
        }
        passing = [n for n, r in rates.items() if r is not None and r >= th.entry_test_min_win_rate]
        checks["entry_test_best_variant"] = best in passing
        entry_rule = {
            "min_gross_win_rate": th.entry_test_min_win_rate,
            "win_rates": rates,
            "variants_passing": passing,
            "hypothesis_closed": not passing,
        }
    decided = [c for c in checks.values() if c is not None]
    outcome = "PROPOSE_VALIDATION" if decided and all(decided) else "CLOSE_ON_DEV"
    return {
        "outcome": outcome,
        "exploratory": pre.exploratory,
        "note": "esplorativa: in validazione l'esito massimo e' INCONCLUSIVE con segno positivo"
        if pre.exploratory
        else None,
        "judged_variants": judged,
        "best_variant": best,
        "best_expectancy_x1": best_exp,
        "positive_pairs": pos_pairs,
        "positive_variants": positive,
        "plateau_needed": need,
        "max_of_k": max_k,
        "entry_test_rule": entry_rule,
        "checks": checks,
        "multiplicity_note": "la correzione vale dentro l'ipotesi, non tra S1, S2, S3 e S7",
    }


def _warnings(
    pre: HypothesisPrereg, paths: dict[str, PricePath], end: datetime, split_end: datetime
) -> list[str]:
    out = []
    if next(iter(paths.values())).provenance.value == "SYNTHETIC":
        out.append("SYNTHETIC data: results say nothing about real markets")
    if end < split_end:
        out.append(f"data ends at {end.isoformat()}, before the end of the split")
    if set(paths) != {"EURUSD", "GBPUSD", "USDJPY"}:
        out.append(f"symbols evaluated: {', '.join(sorted(paths))}")
    return out


def _write_atomic(
    root: Path,
    fingerprint: str,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    tables: dict[str, list[dict[str, Any]]],
) -> None:
    from fxnext.research.hyp.report import render_hypothesis

    root.mkdir(parents=True, exist_ok=True)
    final = root / fingerprint
    tmp = root / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        names = ["report.json", "report.md"]
        if rows:
            pq.write_table(pa.Table.from_pylist(rows), tmp / "outcomes.parquet")
            names.append("outcomes.parquet")
        for name, table in tables.items():
            pq.write_table(pa.Table.from_pylist(table), tmp / f"{name}.parquet")
            names.append(f"{name}.parquet")
        (tmp / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
        (tmp / "report.md").write_text(render_hypothesis(report), encoding="utf-8")
        for name in names:
            with (tmp / name).open("ab") as handle:
                os.fsync(handle.fileno())
        if final.exists():
            shutil.rmtree(tmp)
            return
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


__all__ = ["HypothesisResult", "asdict", "run_hypothesis"]
