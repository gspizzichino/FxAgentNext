"""Run g10-cm-dev-001: data check, 7 variants, random portfolios with max-of-K, criteria.

Output: ``research/<fingerprint>/`` with ``report.json``, ``report.md`` and
``monthly.csv`` (one row per variant and month). Written atomically; an existing
fingerprint is returned as it is.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.domain.hashing import content_hash
from fxnext.research.broker import load_profile
from fxnext.research.g10.panel import Month, Panel, load_panel, split_window
from fxnext.research.g10.portfolio import (
    Market,
    Returns,
    build_market,
    describe,
    random_weights,
    reference_weights,
    simulate,
    target_weights,
)
from fxnext.research.g10.prereg import G10_PROTOCOL_VERSION, G10_SPLITS, G10Prereg, load_g10
from fxnext.research.metrics import percentile_of

G10_RESEARCH_VERSION = "g10-research-v1"
F64 = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class G10Result:
    fingerprint: str
    path: Path
    report: dict[str, Any]


def block_bootstrap_mean_ci(
    x: F64, *, block: int, samples: int, seed: int, level: float = 0.95
) -> tuple[float, float] | None:
    """Percentile CI of the mean with a moving block bootstrap (blocks of ``block`` months)."""

    n = x.size
    if n < max(2, block):
        return None
    rng = np.random.Generator(np.random.PCG64(seed))
    nblocks = math.ceil(n / block)
    means = np.empty(samples)
    chunk = 1000
    offsets = np.arange(block)
    for s in range(0, samples, chunk):
        m = min(chunk, samples - s)
        starts = rng.integers(0, n - block + 1, size=(m, nblocks))
        idx = (starts[:, :, None] + offsets).reshape(m, nblocks * block)[:, :n]
        means[s : s + m] = x[idx].mean(axis=1)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def max_drawdown(x: F64) -> float:
    if x.size == 0:
        return 0.0
    curve = np.concatenate([[0.0], np.cumsum(x)])
    return float((np.maximum.accumulate(curve) - curve).max())


def series_stats(
    x: F64, months: list[Month], pre: G10Prereg, *, with_ci: bool = True
) -> dict[str, Any]:
    n = int(x.size)
    std = float(x.std(ddof=1)) if n > 1 else None
    mean = float(x.mean()) if n else None
    ci = (
        block_bootstrap_mean_ci(
            x,
            block=pre.bootstrap_block_months,
            samples=pre.bootstrap_samples,
            seed=pre.bootstrap_seed,
        )
        if with_ci
        else None
    )
    years: dict[str, dict[str, float]] = {}
    for y in sorted({m[0] for m in months}):
        sel = np.array([m[0] == y for m in months])
        years[str(y)] = {"months": int(sel.sum()), "sum": float(x[sel].sum())}
    subs = {}
    for a, b in pre.criteria.subperiods:
        sel = np.array([a <= m[0] <= b for m in months])
        subs[f"{a}-{b}"] = {
            "months": int(sel.sum()),
            "mean": float(x[sel].mean()) if sel.any() else None,
        }
    return {
        "months": n,
        "mean": mean,
        "std": std,
        "sharpe": (mean / std * math.sqrt(12.0)) if mean is not None and std else None,
        "ci_low": None if ci is None else ci[0],
        "ci_high": None if ci is None else ci[1],
        "total": float(x.sum()),
        "max_drawdown": max_drawdown(x),
        "positive_months": float((x > 0).mean()) if n else None,
        "by_year": years,
        "subperiods": subs,
    }


def _months_of(market: Market, valid: npt.NDArray[np.bool_]) -> list[Month]:
    return [m for m, v in zip(market.months, valid.tolist(), strict=True) if v]


def run_g10(
    prereg_path: Path,
    *,
    cache_root: Path,
    rates_root: Path,
    research_root: Path,
    code_version: str,
    progress: Callable[[str], None] = lambda _m: None,
    check_only: bool = False,
) -> G10Result:
    pre, prereg_sha = load_g10(prereg_path)
    first_month, last_month, _ = split_window(pre.split)
    progress(f"loading daily candles and rates up to {last_month[0]}-{last_month[1]:02d}")
    panel = load_panel(cache_root, rates_root, pre.split)
    quality = panel.quality
    if check_only:
        return G10Result("", Path(), {"split": pre.split, "data_check": quality})
    fingerprint = content_hash(
        "g10-run",
        {
            "prereg_sha256": prereg_sha,
            "raw": panel.raw_hashes,
            "rates": panel.rate_hashes,
            "code_version": code_version,
            "research_version": G10_RESEARCH_VERSION,
        },
    )
    final = Path(research_root) / fingerprint
    if (final / "report.json").exists():
        return G10Result(fingerprint, final, json.loads((final / "report.json").read_text()))
    market = build_market(panel, first_month, last_month, pre.costs, pre.rate_lag_months)
    report: dict[str, Any] = {
        "experiment_id": pre.experiment_id,
        "hypothesis": pre.hypothesis,
        "split": pre.split,
        "split_bounds": [d.isoformat() for d in G10_SPLITS[pre.split]],
        "fingerprint": fingerprint,
        "prereg_sha256": prereg_sha,
        "frozen_text_sha256": pre.frozen_text_sha256,
        "versions": {
            "protocol": G10_PROTOCOL_VERSION,
            "research": G10_RESEARCH_VERSION,
            "costs": pre.costs.version,
            "code": code_version,
        },
        "broker_profile": _broker_profile(pre),
        "inputs": {"raw_files": len(panel.raw_hashes), "rates": panel.rate_hashes},
        "data_check": quality,
        "periods": len(market.months),
        "non_contiguous_periods": [
            f"{m[0]}-{m[1]:02d}"
            for m, c in zip(market.months, market.contiguous.tolist(), strict=True)
            if not c
        ],
        "zero_spread_filled": market.zero_spread_filled,
        "variants": {},
        "warnings": [],
    }
    rows: list[dict[str, Any]] = []
    real: dict[str, tuple[Returns, npt.NDArray[np.bool_]]] = {}
    actives: dict[str, npt.NDArray[np.bool_]] = {}
    for v in pre.variants:
        progress(f"variant {v.name}")
        weights, active, reasons = target_weights(panel, market, v)
        ret = simulate(market, weights, pre.costs)
        valid = active & market.contiguous
        real[v.name] = (ret, valid)
        actives[v.name] = active
        months = _months_of(market, valid)
        entry: dict[str, Any] = {
            "signal": v.signal,
            "k": v.k,
            "lookback_months": v.lookback_months,
            "excluded_months": reasons,
            "net": series_stats(ret.net(1.0)[valid], months, pre),
            "net_no_markup": series_stats(ret.net(1.0, markup=False)[valid], months, pre),
            "gross": series_stats(ret.gross[valid], months, pre),
            "stress": {
                str(mult): series_stats(ret.net(mult)[valid], months, pre, with_ci=False)
                for mult in pre.cost_multipliers
            },
            "components_mean": {
                "fx": float(ret.fx[valid].mean()) if valid.any() else None,
                "carry": float(ret.carry[valid].mean()) if valid.any() else None,
                "spread": float(ret.spread[valid].mean()) if valid.any() else None,
                "fees": float(ret.fees[valid].mean()) if valid.any() else None,
                "markup": float(ret.markup[valid].mean()) if valid.any() else None,
            },
            "turnover_mean": float(ret.turnover[valid].mean()) if valid.any() else None,
            "cost_mean": float((ret.gross - ret.net(1.0))[valid].mean()) if valid.any() else None,
        }
        report["variants"][v.name] = entry
        for j, pos in enumerate(describe(weights)):
            rows.append(
                {
                    "variant": v.name,
                    "month": f"{market.months[j][0]}-{market.months[j][1]:02d}",
                    "valid": bool(valid[j]),
                    "long": " ".join(pos["long"]),
                    "short": " ".join(pos["short"]),
                    "gross": float(ret.gross[j]),
                    "fx": float(ret.fx[j]),
                    "carry": float(ret.carry[j]),
                    "spread": float(ret.spread[j]),
                    "fees": float(ret.fees[j]),
                    "markup": float(ret.markup[j]),
                    "net": float(ret.net(1.0)[j]),
                    "turnover": float(ret.turnover[j]),
                }
            )
    progress("random portfolios")
    rng = np.random.Generator(np.random.PCG64(pre.benchmark_seed))
    rand_means: dict[str, F64] = {}
    for v in pre.variants:
        ret, valid = real[v.name]
        w = random_weights(market, actives[v.name], v.k, pre.benchmark_repetitions, rng)
        rr = simulate(market, w, pre.costs)
        net = rr.net(1.0)[:, valid]
        rand_means[v.name] = net.mean(axis=1) if valid.any() else np.full(len(w), np.nan)
        mean = report["variants"][v.name]["net"]["mean"]
        dist = rand_means[v.name]
        report["variants"][v.name]["random"] = {
            "p5": float(np.percentile(dist, 5)),
            "p50": float(np.percentile(dist, 50)),
            "p95": float(np.percentile(dist, pre.criteria.benchmark_percentile)),
            "percentile_of_variant": None if mean is None else percentile_of(mean, dist),
        }
    max_of_k = np.max(np.vstack([rand_means[v.name] for v in pre.variants]), axis=0)
    ref_w, ref_active = reference_weights(market)
    ref = simulate(market, ref_w, pre.costs)
    ref_valid = ref_active & market.contiguous
    ref_months = _months_of(market, ref_valid)
    report["reference_long_all_vs_usd"] = {
        "net": series_stats(ref.net(1.0)[ref_valid], ref_months, pre, with_ci=False),
        "gross": series_stats(ref.gross[ref_valid], ref_months, pre, with_ci=False),
    }
    report["max_of_k"] = {
        "k": len(pre.variants),
        "p50": float(np.percentile(max_of_k, 50)),
        "p95": float(np.percentile(max_of_k, pre.criteria.benchmark_percentile)),
    }
    report["assessment"] = assess(pre, report, max_of_k)
    report["warnings"] = _warnings(report)
    _write_atomic(Path(research_root), fingerprint, report, rows)
    return G10Result(fingerprint, final, report)


def assess(pre: G10Prereg, report: dict[str, Any], max_of_k: F64) -> dict[str, Any]:
    variants = report["variants"]
    means = {name: v["net"]["mean"] for name, v in variants.items()}
    ranked = [n for n in (v.name for v in pre.variants) if means[n] is not None]
    if not ranked:
        return {"outcome": "NO_DATA", "checks": {}}
    best = max(ranked, key=lambda n: (means[n], -ranked.index(n)))
    b = variants[best]
    crit = pre.criteria
    if pre.split == "development":
        p95_max = float(np.percentile(max_of_k, crit.benchmark_percentile))
        positive = sum(1 for n in ranked if means[n] > 0)
        subs_pos = sum(
            1 for s in b["net"]["subperiods"].values() if s["mean"] is not None and s["mean"] > 0
        )
        checks = {
            "best_net_positive": means[best] > 0,
            "best_above_max_of_k_p95": means[best] > p95_max,
            "positive_variants": positive >= crit.min_positive_variants,
            "positive_subperiods": subs_pos >= crit.min_positive_subperiods,
        }
        return {
            "outcome": "PROPOSE_VALIDATION" if all(checks.values()) else "CLOSE_ON_DEV",
            "best_variant": best,
            "best_mean": means[best],
            "max_of_k_p95": p95_max,
            "positive_variants": positive,
            "positive_subperiods": subs_pos,
            "checks": checks,
        }
    stress = b["stress"].get(str(crit.validation_stress_multiplier), {}).get("mean")
    first = {
        "net_positive": means[best] > 0,
        "above_random_p95": means[best] > b["random"]["p95"],
        "stress_positive": stress is not None and stress > 0,
    }
    ci_above = b["net"]["ci_low"] is not None and b["net"]["ci_low"] > 0
    outcome = ("PASSED" if ci_above else "INCONCLUSIVE") if all(first.values()) else "REJECTED"
    return {
        "outcome": outcome,
        "best_variant": best,
        "checks": {**first, "ci_above_zero": ci_above},
    }


def _warnings(report: dict[str, Any]) -> list[str]:
    out = []
    q = report["data_check"]
    if q.get("months_without_rebalance"):
        out.append(f"months without a rebalance day: {len(q['months_without_rebalance'])}")
    if report["non_contiguous_periods"]:
        out.append(f"periods spanning more than one month: {report['non_contiguous_periods']}")
    for name, v in report["variants"].items():
        if v["excluded_months"]:
            out.append(f"{name}: {len(v['excluded_months'])} months flat and excluded")
    return out


def _write_atomic(
    root: Path, fingerprint: str, report: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    from fxnext.research.g10.report import render_g10

    root.mkdir(parents=True, exist_ok=True)
    final = root / fingerprint
    tmp = root / f".tmp-{fingerprint}-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        (tmp / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=_json_default), encoding="utf-8"
        )
        (tmp / "report.md").write_text(render_g10(report), encoding="utf-8")
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        (tmp / "monthly.csv").write_text(buf.getvalue(), encoding="utf-8")
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _json_default(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"not serialisable: {type(value)}")


__all__ = ["G10Result", "Panel", "run_g10", "assess", "block_bootstrap_mean_ci"]


def _broker_profile(pre: G10Prereg) -> dict[str, str] | None:
    """Name and sha256 of the measured cost profile (g10-cost-naga-v1), checked on load."""

    if pre.costs.broker_profile is None:
        return None
    prof = load_profile(pre.costs.broker_profile)
    return {"name": prof.name, "sha256": prof.sha256}
