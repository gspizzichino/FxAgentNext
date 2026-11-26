"""Data check and the single run of l1-composito-001 (M10).

``check`` reports coverage only (dates, counts, chosen series, eligible
currencies): no price change, return or signal value. ``run`` refuses to start if
the experiment already has a result (one variant, one run) or if the test would
start after 1985 (frozen text §6).
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from fxnext.domain.model import Model
from fxnext.research.broker import load_profile
from fxnext.research.composite.model import (
    NC,
    Market,
    Month,
    add_months,
    build_market,
    build_panel,
    composite_weights,
    month_range,
    random_weights,
    simulate,
    stats,
)
from fxnext.research.composite.sources import (
    CURRENCIES,
    DEM_SERIES,
    FX_SERIES,
    LAST_DAY,
    MACRO_ORDER,
    RATE_SERIES,
    VARIABLES,
    Series,
    all_fred_series,
    load_series,
)
from fxnext.research.protocol import ProtocolError

L1_PROTOCOL_VERSION = "l1-protocol-v1"
FIRST_MONTH: Month = (1976, 1)
LAST_MONTH: Month = (LAST_DAY.year, LAST_DAY.month)
PAIRS = {
    "EUR": "EURUSD",
    "GBP": "GBPUSD",
    "AUD": "AUDUSD",
    "NZD": "NZDUSD",
    "JPY": "USDJPY",
    "CHF": "USDCHF",
    "CAD": "USDCAD",
    "NOK": "USDNOK",
    "SEK": "USDSEK",
}


class L1Prereg(Model):
    experiment_id: str
    hypothesis: str
    kind: str = "l1_composite_filter"
    frozen_text: str
    frozen_text_sha256: str
    broker_profile: str = "naga-demo-2026-11-18"
    rate_lag_months: int = 1
    min_currencies: int = 7
    latest_start: tuple[int, int] = (1985, 12)
    t_threshold: float = 2.0
    newey_west_lags: int = 6
    stress_multiplier: float = 1.5
    benchmark_repetitions: int = 1000
    benchmark_percentile: float = 95.0
    benchmark_seed: int = 20261126


def load_l1(path: Path) -> tuple[L1Prereg, str]:
    raw = Path(path).read_bytes()
    pre = L1Prereg.model_validate_json(json.dumps(tomllib.loads(raw.decode("utf-8"))))
    if pre.kind != "l1_composite_filter":
        raise ProtocolError("not an l1 composite pre-registration")
    if pre.rate_lag_months < 1:
        raise ProtocolError("rates need at least one month of publication lag")
    return pre, hashlib.sha256(raw).hexdigest()


def load_available(root: Path) -> tuple[dict[str, Series], dict[str, str], list[str]]:
    available: dict[str, Series] = {}
    hashes: dict[str, str] = {}
    missing: list[str] = []
    for sid in [*all_fred_series(), DEM_SERIES]:
        got = load_series(root, sid)
        if got is None:
            missing.append(sid)
            continue
        available[sid], hashes[sid] = got
    return available, hashes, missing


def _iso(m: Month) -> str:
    return f"{m[0]}-{m[1]:02d}"


def coverage(available: dict[str, Series], missing: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"not_downloaded_or_not_found": sorted(missing), "series": {}}
    for sid, s in sorted(available.items()):
        days = sorted(s.obs)
        out["series"][sid] = {
            "frequency": s.frequency,
            "observations": len(days),
            "first": days[0].isoformat() if days else None,
            "last": days[-1].isoformat() if days else None,
        }
    return out


@dataclass
class L1Result:
    report: dict[str, Any]
    path: Path | None
    fingerprint: str | None


def _market(pre: L1Prereg, available: dict[str, Series]) -> tuple[Market, dict[str, Any]]:
    panel = build_panel(available, (FIRST_MONTH[0] - 6, 1), LAST_MONTH)
    profile = load_profile(pre.broker_profile)
    spreads = {}
    for ccy, pair in PAIRS.items():
        pips = profile.spread_pips(pair)
        if pips is None:
            raise ProtocolError(f"{pair}: no spread in profile {pre.broker_profile}")
        spreads[ccy] = float(pips)
    # signal months: t, held over t+1; the last holding month is LAST_MONTH
    signal_months = month_range(add_months(FIRST_MONTH, -1), add_months(LAST_MONTH, -1))
    market = build_market(panel, signal_months, spreads, pre.rate_lag_months)
    counts = market.eligible_counts
    start_idx = next((j for j, n in enumerate(counts) if n >= pre.min_currencies), None)
    info: dict[str, Any] = {
        "chosen_macro_series": panel.chosen,
        "rate_series": {c: [s for s, _ in RATE_SERIES[c]] for c in MACRO_ORDER},
        "fx_series": {c: FX_SERIES[c][0] for c in CURRENCIES} | {"DEM (to 1998)": DEM_SERIES},
        "spread_pips": spreads,
        "markup_pct_per_side": profile.markup_pct_by_currency(),
        "macro_missing": {
            c: [v for v in VARIABLES if v not in panel.macro[c]] for c in MACRO_ORDER
        },
    }
    if start_idx is None:
        info["first_holding_month"] = None
        return market, info
    first_hold = add_months(market.signal_months[start_idx], 1)
    info["first_holding_month"] = _iso(first_hold)
    info["start_after_limit"] = first_hold > pre.latest_start
    later = counts[start_idx:]
    info["eligible_after_start"] = {"min": min(later), "max": max(later)}
    info["months_below_minimum"] = [
        _iso(add_months(m, 1))
        for m, n in zip(market.signal_months[start_idx:], later, strict=True)
        if n < pre.min_currencies
    ]
    by_year: dict[str, list[int]] = {}
    for m, n in zip(market.signal_months, counts, strict=True):
        by_year.setdefault(str(add_months(m, 1)[0]), []).append(n)
    info["eligible_by_year"] = {y: [min(v), max(v)] for y, v in by_year.items()}
    eligible_ccy = {
        CURRENCIES[c]: _iso(
            add_months(market.signal_months[int(np.argmax(market.eligible[:, c]))], 1)
        )
        for c in range(NC)
        if market.eligible[:, c].any()
    }
    info["first_eligible_month_by_currency"] = eligible_ccy
    info["start_index"] = start_idx
    return market, info


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{path.name}-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_l1(
    prereg_path: Path,
    *,
    data_root: Path,
    research_root: Path,
    code_version: str,
    check_only: bool,
    progress: Callable[[str], None] | None = None,
) -> L1Result:
    pre, pre_sha = load_l1(prereg_path)
    available, hashes, missing = load_available(data_root)
    market, info = _market(pre, available)
    report: dict[str, Any] = {
        "experiment_id": pre.experiment_id,
        "protocol_version": L1_PROTOCOL_VERSION,
        "prereg_sha256": pre_sha,
        "frozen_text": pre.frozen_text,
        "frozen_text_sha256": pre.frozen_text_sha256,
        "data": coverage(available, missing),
        "setup": {k: v for k, v in info.items() if k != "start_index"},
    }
    if check_only:
        return L1Result(report, None, None)
    out_dir = Path(research_root) / pre.experiment_id
    if out_dir.exists() and any(out_dir.glob("*/report.json")):
        raise ProtocolError(f"{pre.experiment_id} already has a result: one run only")
    if info.get("first_holding_month") is None:
        raise ProtocolError(f"never {pre.min_currencies} eligible currencies: nothing to run")
    if info["start_after_limit"]:
        raise ProtocolError(
            f"the test would start in {info['first_holding_month']}, after "
            f"{_iso(pre.latest_start)}: stop and decide (frozen text §6)"
        )
    j0 = int(info["start_index"])
    sub = Market(
        market.signal_months[j0:],
        market.composite[j0:],
        market.eligible[j0:],
        market.ret[j0:],
        market.half_spread[j0:],
        market.eligible_counts[j0:],
    )
    markup = np.array([info["markup_pct_per_side"][c] for c in CURRENCIES])
    min_count = 2
    w = composite_weights(sub, min_count)
    r = simulate(sub, w, markup)
    lags = pre.newey_west_lags
    net1, net_s = r.net(1.0), r.net(pre.stress_multiplier)
    half = net1.size // 2
    if progress:
        progress(f"random benchmark: {pre.benchmark_repetitions} portfolios")
    rng = np.random.default_rng(pre.benchmark_seed)
    bench = np.array(
        [
            float(simulate(sub, random_weights(sub, min_count, rng), markup).net(1.0).mean())
            for _ in range(pre.benchmark_repetitions)
        ]
    )
    p95 = float(np.percentile(bench, pre.benchmark_percentile))
    s1 = stats(net1, lags)
    t_nw = s1.get("t_newey_west")
    checks = {
        "1_mean_net_positive": bool(net1.mean() > 0),
        "2_t_newey_west_at_least": bool(t_nw is not None and t_nw >= pre.t_threshold),
        "3_both_halves_positive": bool(net1[:half].mean() > 0 and net1[half:].mean() > 0),
        "4_positive_with_stress": bool(net_s.mean() > 0),
        "5_above_random_percentile": bool(net1.mean() > p95),
    }
    holding = [add_months(m, 1) for m in sub.signal_months]
    report["results"] = {
        "holding_months": [_iso(holding[0]), _iso(holding[-1])],
        "gross": stats(r.gross, lags),
        "net": s1,
        "net_stress": stats(net_s, lags),
        "cost_pct_year": {
            "markup": float(r.markup.mean()) * 1200.0,
            "spread": float(r.spread.mean()) * 1200.0,
        },
        "halves_mean_net_pct_month": [
            float(net1[:half].mean()) * 100.0,
            float(net1[half:].mean()) * 100.0,
        ],
        "halves": [
            [_iso(holding[0]), _iso(holding[half - 1])],
            [_iso(holding[half]), _iso(holding[-1])],
        ],
        "random_benchmark": {
            "repetitions": pre.benchmark_repetitions,
            "percentile": pre.benchmark_percentile,
            "value_pct_month": p95 * 100.0,
        },
        "mean_gross_exposure": float(np.abs(w).sum(axis=1).mean()),
        "by_year_net_pct": {
            str(y): float(net1[[i for i, m in enumerate(holding) if m[0] == y]].sum()) * 100.0
            for y in sorted({m[0] for m in holding})
        },
    }
    report["assessment"] = {
        "checks": checks,
        "outcome": "PASSA" if all(checks.values()) else "FALLITO",
    }
    fp_src = json.dumps(
        {"prereg": pre_sha, "data": hashes, "code": code_version}, sort_keys=True
    ).encode()
    fingerprint = hashlib.sha256(fp_src).hexdigest()
    report["fingerprint"] = fingerprint
    report["code_version"] = code_version
    report["run_at"] = datetime.now(UTC).isoformat()
    path = out_dir / fingerprint[:16] / "report.json"
    _write_atomic(path, json.dumps(report, indent=2, sort_keys=True, default=str))
    _write_atomic(path.with_name("report.md"), render(report))
    return L1Result(report, path, fingerprint)


def _f(x: Any, d: int = 2) -> str:
    return "—" if x is None else f"{x:.{d}f}".replace(".", ",")


def render(r: dict[str, Any]) -> str:
    res, a = r["results"], r["assessment"]
    lines = [
        f"# {r['experiment_id']}: esito {a['outcome']}",
        "",
        f"Mesi di tenuta {res['holding_months'][0]} → {res['holding_months'][1]}; "
        f"fingerprint `{r['fingerprint'][:16]}`.",
        "",
        "| Serie | Media %/anno | Dev. st. %/anno | Sharpe | t NW | Max DD % |",
        "|---|---|---|---|---|---|",
    ]
    for key, label in (("gross", "Lordo"), ("net", "Netto Naga ×1"), ("net_stress", "Netto ×1,5")):
        s = res[key]
        lines.append(
            f"| {label} | {_f(s['mean_pct_year'])} | {_f(s['sd_pct_year'])} | "
            f"{_f(s['sharpe_year'])} | {_f(s['t_newey_west'])} | {_f(s['max_drawdown_pct'])} |"
        )
    lines += ["", "Criteri:", ""]
    lines += [f"- {k}: {'sì' if v else 'no'}" for k, v in a["checks"].items()]
    lines += [
        "",
        f"Costi medi %/anno: ricarico {_f(res['cost_pct_year']['markup'])}, "
        f"spread {_f(res['cost_pct_year']['spread'])}. "
        f"Benchmark casuale p{_f(res['random_benchmark']['percentile'], 0)}: "
        f"{_f(res['random_benchmark']['value_pct_month'], 3)} %/mese.",
        "",
    ]
    return "\n".join(lines)
