"""Run h1-cbdrift-001: data check, events, random-direction benchmark, criteria.

Output: ``research/<fingerprint>/`` with ``report.json``, ``report.md`` and
``events.csv``. Written atomically; an existing fingerprint is returned as is.
``check_only`` stops after the data check, before any return is computed.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import shutil
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from fxnext.data.daily import G10_DIGITS, DailyCache, decode_daily
from fxnext.data.dukascopy import FetchStatus
from fxnext.domain.enums import PriceBasis
from fxnext.domain.hashing import content_hash
from fxnext.research.cbdrift.events import (
    Event,
    FxPanel,
    Meeting,
    RateGrid,
    admission,
    build_events,
    traded,
    weekdays,
)
from fxnext.research.cbdrift.prereg import (
    BANKS,
    CBDRIFT_PROTOCOL_VERSION,
    CBDRIFT_SPLITS,
    COST_MULTIPLIERS,
    FX_FIRST_YEAR,
    PAIRS,
    RATE_WARMUP_START,
    CbdriftPrereg,
    load_cbdrift,
)
from fxnext.research.cbdrift.sources import load_calendar, load_rates
from fxnext.research.metrics import percentile_of

CBDRIFT_RESEARCH_VERSION = "cbdrift-research-v1"
F64 = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class CbdriftResult:
    fingerprint: str
    path: Path
    report: dict[str, Any]


def load_fx(cache_root: Path, split: str, currencies: list[str]) -> FxPanel:
    """Daily BID/ASK closes of the pairs, weekdays from 2003 to the end of the split only."""

    _, end = CBDRIFT_SPLITS[split]
    days = weekdays(date(FX_FIRST_YEAR, 1, 1), end)
    row = {d: i for i, d in enumerate(days)}
    cache = DailyCache(cache_root)
    resolved = cache.resolved()
    last_year = days[-1].year
    bid: dict[str, F64] = {}
    ask: dict[str, F64] = {}
    hashes: list[str] = []
    files: Counter[str] = Counter()
    pairs: dict[str, Any] = {}
    for ccy in currencies:
        symbol = PAIRS[ccy]
        close: dict[str, F64] = {}
        vol: dict[str, F64] = {}
        for basis in (PriceBasis.BID, PriceBasis.ASK):
            c = np.full(len(days), np.nan)
            v = np.zeros(len(days))
            for year in range(FX_FIRST_YEAR, last_year + 1):
                rec = resolved.get((symbol, basis.value, year))
                if rec is None:
                    files["missing"] += 1
                    continue
                if rec["status"] != FetchStatus.OK:
                    files[str(rec["status"]).lower()] += 1
                    continue
                files["ok"] += 1
                hashes.append(rec["sha256"])
                payload = (cache.root / rec["cache_path"]).read_bytes()
                for candle in decode_daily(payload, year, G10_DIGITS[symbol]):
                    i = row.get(candle.day)
                    if i is not None:
                        c[i] = candle.close
                        v[i] = candle.volume
            close[basis.value], vol[basis.value] = c, v
        b, a = close["BID"], close["ASK"]
        missing = np.isnan(b) | np.isnan(a)
        flat = ~missing & ((vol["BID"] <= 0) | (vol["ASK"] <= 0))
        crossed = ~missing & ~flat & ~((b > 0) & (a >= b))
        bad = missing | flat | crossed
        bid[ccy] = np.where(bad, np.nan, b)
        ask[ccy] = np.where(bad, np.nan, a)
        zero = ~bad & (a == b)
        pairs[symbol] = {
            "weekdays": len(days),
            "valid": int((~bad).sum()),
            "missing": int(missing.sum()),
            "zero_volume": int(flat.sum()),
            "crossed": int(crossed.sum()),
            "zero_spread": int(zero.sum()),
        }
    return FxPanel(days, bid, ask, sorted(hashes), {"files": dict(files), "pairs": pairs})


def data_check(
    split: str, meetings: list[Meeting], grid: RateGrid, rate_info: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    admitted, info = admission(grid, split)
    start, end = CBDRIFT_SPLITS[split]
    per_bank: dict[str, dict[str, int]] = {}
    for m in meetings:
        if not start.year - 1 <= m.local_date.year < end.year + 1:
            continue
        b = per_bank.setdefault(m.bank, {})
        key = str(m.local_date.year)
        b[key] = b.get(key, 0) + 1
    unscheduled = [
        f"{m.bank} {m.local_date.isoformat()}"
        for m in meetings
        if not m.scheduled and start <= m.local_date < end
    ]
    return admitted, {
        "split": split,
        "split_bounds": [start.isoformat(), end.isoformat()],
        "admission": info,
        "admitted": admitted,
        "meetings_per_bank_year": per_bank,
        "unscheduled_in_split": unscheduled,
        "rates": rate_info,
    }


def run_cbdrift(
    prereg_path: Path,
    *,
    cache_root: Path,
    rates_root: Path,
    calendar_path: Path,
    research_root: Path,
    code_version: str,
    progress: Callable[[str], None] = lambda _m: None,
    check_only: bool = False,
) -> CbdriftResult:
    pre, prereg_sha = load_cbdrift(prereg_path)
    start, end = CBDRIFT_SPLITS[pre.split]
    progress(f"loading rates and calendar up to {end.isoformat()} (excluded)")
    series, rate_hashes, rate_info = load_rates(rates_root, end)
    grid = RateGrid.build(series, weekdays(RATE_WARMUP_START, end))
    meetings, calendar_sha = load_calendar(calendar_path)
    admitted, check = data_check(pre.split, meetings, grid, rate_info)
    check["calendar_sha256"] = calendar_sha
    progress(f"loading daily candles of {', '.join(PAIRS[c] for c in admitted)}")
    fx = load_fx(cache_root, pre.split, admitted)
    check["fx"] = fx.quality
    if check_only:
        return CbdriftResult("", Path(), {"split": pre.split, "data_check": check})
    fingerprint = content_hash(
        "cbdrift-run",
        {
            "prereg_sha256": prereg_sha,
            "raw": fx.raw_hashes,
            "rates": rate_hashes,
            "calendar": calendar_sha,
            "code_version": code_version,
            "research_version": CBDRIFT_RESEARCH_VERSION,
        },
    )
    final = Path(research_root) / fingerprint
    if (final / "report.json").exists():
        return CbdriftResult(fingerprint, final, json.loads((final / "report.json").read_text()))
    progress("events")
    events, info = build_events(meetings, pre.split, fx, grid, admitted)
    report = summarize(pre, events, info)
    report.update(
        {
            "experiment_id": pre.experiment_id,
            "hypothesis": pre.hypothesis,
            "split": pre.split,
            "split_bounds": [start.isoformat(), end.isoformat()],
            "fingerprint": fingerprint,
            "prereg_sha256": prereg_sha,
            "frozen_text_sha256": pre.frozen_text_sha256,
            "validation_approval": pre.validation_approval,
            "versions": {
                "protocol": CBDRIFT_PROTOCOL_VERSION,
                "research": CBDRIFT_RESEARCH_VERSION,
                "code": code_version,
            },
            "inputs": {"fx_files": len(fx.raw_hashes), "rates": rate_hashes},
            "data_check": check,
        }
    )
    _write_atomic(Path(research_root), fingerprint, report, events)
    return CbdriftResult(fingerprint, final, report)


def month_block_ci(
    x: F64, months: list[tuple[int, int]], *, samples: int, seed: int, level: float = 0.95
) -> tuple[float, float] | None:
    """Percentile CI of the mean per event, resampling calendar months with replacement."""

    if x.size < 2:
        return None
    keys = sorted(set(months))
    groups = [np.array([i for i, m in enumerate(months) if m == k]) for k in keys]
    sums = np.array([x[g].sum() for g in groups])
    counts = np.array([g.size for g in groups], dtype=float)
    rng = np.random.Generator(np.random.PCG64(seed))
    means = np.empty(samples)
    chunk = 1000
    for s in range(0, samples, chunk):
        m = min(chunk, samples - s)
        idx = rng.integers(0, len(keys), size=(m, len(keys)))
        means[s : s + m] = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def stats(x: F64) -> dict[str, Any]:
    n = int(x.size)
    mean = float(x.mean()) if n else None
    std = float(x.std(ddof=1)) if n > 1 else None
    return {
        "events": n,
        "mean": mean,
        "std": std,
        "t": (mean / (std / math.sqrt(n))) if mean is not None and std else None,
        "positive_share": float((x > 0).mean()) if n else None,
        "total": float(x.sum()),
    }


def _arr(events: list[Event], f: Callable[[Event], float]) -> F64:
    return np.array([f(e) for e in events], dtype=float)


def summarize(pre: CbdriftPrereg, events: list[Event], info: dict[str, Any]) -> dict[str, Any]:
    tr = traded(events)
    months = [(e.entry_day.year, e.entry_day.month) for e in tr if e.entry_day]
    net = _arr(tr, lambda e: e.net(1.0))
    ci = month_block_ci(net, months, samples=pre.bootstrap_samples, seed=pre.bootstrap_seed)
    main = stats(net)
    main["ci_low"], main["ci_high"] = (None, None) if ci is None else ci
    out: dict[str, Any] = {
        "counts": {
            "meetings_considered": len(events),
            "no_position": sum(1 for e in events if e.excluded is None and e.direction == 0),
            "hikes_expected": sum(1 for e in tr if e.direction > 0),
            "cuts_expected": sum(1 for e in tr if e.direction < 0),
            "excluded": info["excluded"],
            "zero_spread_filled": sum(e.zero_spread_filled for e in tr),
        },
        "net": main,
        "gross": stats(_arr(tr, lambda e: e.gross)),
        "net_no_markup": stats(_arr(tr, lambda e: e.net(1.0, markup=False))),
        "stress": {str(m): stats(np.array([e.net(m) for e in tr])) for m in COST_MULTIPLIERS},
        "components_mean": {
            k: (float(np.mean([getattr(e, k) for e in tr])) if tr else None)
            for k in ("fx", "carry", "spread", "slippage", "markup")
        },
    }
    out["by_direction"] = {
        name: stats(_arr([e for e in tr if e.direction == d], lambda e: e.net(1.0)))
        for name, d in (("hikes", 1), ("cuts", -1))
    }
    out["by_bank"] = {
        b: stats(_arr([e for e in tr if e.bank == b], lambda e: e.net(1.0))) for b in BANKS
    }
    years = sorted({e.entry_day.year for e in tr if e.entry_day})
    out["by_year"] = {
        str(y): stats(
            _arr([e for e in tr if e.entry_day and e.entry_day.year == y], lambda e: e.net(1.0))
        )
        for y in years
    }
    out["subperiods"] = {
        f"{a}-{b}": stats(
            _arr(
                [e for e in tr if e.entry_day and a <= e.entry_day.year <= b], lambda e: e.net(1.0)
            )
        )
        for a, b in pre.criteria.subperiods
    }
    out["random_direction"] = random_direction(pre, tr)
    out["reference_long_currency"] = {
        "net": stats(_arr(tr, lambda e: e.long_fx + e.long_carry - e.cost(1.0))),
    }
    out["assessment"] = assess(pre, out)
    return out


def random_direction(pre: CbdriftPrereg, tr: list[Event]) -> dict[str, Any]:
    if not tr:
        return {"p95": None, "percentile_of_rule": None}
    rng = np.random.Generator(np.random.PCG64(pre.benchmark_seed))
    move = _arr(tr, lambda e: e.long_fx + e.long_carry)
    cost = _arr(tr, lambda e: e.cost(1.0))
    signs = rng.choice(np.array([-1.0, 1.0]), size=(pre.benchmark_repetitions, len(tr)))
    means = (signs * move - cost).mean(axis=1)
    real = float((_arr(tr, lambda e: e.net(1.0))).mean())
    return {
        "repetitions": pre.benchmark_repetitions,
        "p5": float(np.percentile(means, 5)),
        "p50": float(np.percentile(means, 50)),
        "p95": float(np.percentile(means, pre.criteria.benchmark_percentile)),
        "percentile_of_rule": percentile_of(real, means),
    }


def assess(pre: CbdriftPrereg, r: dict[str, Any]) -> dict[str, Any]:
    mean = r["net"]["mean"]
    if mean is None:
        return {"outcome": "NO_DATA", "checks": {}}
    crit = pre.criteria
    stress = r["stress"][str(crit.stress_multiplier)]["mean"]
    first = {
        "net_positive": mean > 0,
        "above_random_p95": mean > r["random_direction"]["p95"],
        "stress_1_5_positive": stress is not None and stress > 0,
    }
    if pre.split == "development":
        eligible = {
            k: s for k, s in r["subperiods"].items() if s["events"] >= crit.min_events_subperiod
        }
        positive = sum(1 for s in eligible.values() if s["mean"] is not None and s["mean"] > 0)
        checks = {**first, "positive_subperiods": positive >= crit.min_positive_subperiods}
        return {
            "outcome": "PROPOSE_VALIDATION" if all(checks.values()) else "CLOSE_ON_DEV",
            "eligible_subperiods": sorted(eligible),
            "positive_subperiods": positive,
            "checks": checks,
        }
    ci_above = r["net"]["ci_low"] is not None and r["net"]["ci_low"] > 0
    outcome = ("PASSED" if ci_above else "INCONCLUSIVE") if all(first.values()) else "REJECTED"
    return {"outcome": outcome, "checks": {**first, "ci_above_zero": ci_above}}


def _write_atomic(
    root: Path, fingerprint: str, report: dict[str, Any], events: list[Event]
) -> None:
    from fxnext.research.cbdrift.report import render_cbdrift

    root.mkdir(parents=True, exist_ok=True)
    final = root / fingerprint
    tmp = root / f".tmp-{fingerprint}-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        (tmp / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=_json_default), encoding="utf-8"
        )
        (tmp / "report.md").write_text(render_cbdrift(report), encoding="utf-8")
        buf = io.StringIO()
        fields = [
            "bank", "currency", "announcement", "t_day", "decision", "signal_day", "entry_day",
            "exit_day", "expected_bp", "direction", "position", "excluded", "legs", "fx", "carry",
            "spread", "slippage", "markup", "gross", "net",
        ]  # fmt: skip
        w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for e in events:
            live = e.excluded is None and e.direction != 0
            w.writerow(
                {
                    "bank": e.bank,
                    "currency": e.currency,
                    "announcement": e.announcement.isoformat(),
                    "t_day": e.t_day.isoformat(),
                    "decision": e.decision,
                    "signal_day": e.signal_day.isoformat() if e.signal_day else "",
                    "entry_day": e.entry_day.isoformat() if e.entry_day else "",
                    "exit_day": e.exit_day.isoformat() if e.exit_day else "",
                    "expected_bp": "" if e.expected_bp is None else f"{e.expected_bp:.2f}",
                    "direction": e.direction,
                    "position": e.position,
                    "excluded": e.excluded or "",
                    "legs": " ".join(e.legs),
                    "fx": e.fx if live else "",
                    "carry": e.carry if live else "",
                    "spread": e.spread if live else "",
                    "slippage": e.slippage if live else "",
                    "markup": e.markup if live else "",
                    "gross": e.gross if live else "",
                    "net": e.net(1.0) if live else "",
                }
            )
        (tmp / "events.csv").write_text(buf.getvalue(), encoding="utf-8")
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
