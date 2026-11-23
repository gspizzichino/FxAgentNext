"""Confirmation run of e1-gotobi-001 (frozen text m8/prereg-e1-gotobi-001-congelato-2026-11-23).

Execution follows the M2 fill rules without changes: buy at the ASK close of the entry minute,
sell at the BID close of the exit minute. The entry minute must be VALID (ticks observed);
the exit minute may be VALID or FLAT (M2: flat minutes are usable for exits, not for entry).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from fxnext.data.path import MinuteStatus, PricePath, load_path
from fxnext.research.gotobi.calendar import control_days, gotobi_days

PROTOCOL = "gotobi-protocol-v1"
PIP = 0.01
SLIPPAGE_PIPS_PER_SIDE = 0.1
NAGA_MIN_SPREAD_PIPS_RT = 0.9
BOOT_N = 10_000
SEED_CI = 20261123
SEED_RANDOM_DAYS = 20261124
# Confirmation windows (frozen text §2). Anything else is refused.
WINDOWS = ((date(2008, 1, 1), date(2015, 1, 1)), (date(2024, 1, 1), date(2026, 11, 1)))
_NS = 1_000_000_000


class GotobiError(ValueError):
    pass


@dataclass(frozen=True)
class Prereg:
    experiment_id: str
    frozen_text_sha256: str
    paths: tuple[str, ...]
    sha256: str


def load_prereg(file: Path) -> Prereg:
    raw = file.read_bytes()
    doc = tomllib.loads(raw.decode())
    if doc.get("experiment_id") != "e1-gotobi-001":
        raise GotobiError("unknown experiment_id")
    paths = tuple(doc.get("paths", ()))
    return Prereg(
        doc["experiment_id"], doc["frozen_text_sha256"], paths, hashlib.sha256(raw).hexdigest()
    )


def _ns(d: date, hh: int, mm: int) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC).timestamp()) * _NS


def window_minutes(d: date) -> tuple[int, int]:
    """Entry: minute 22:59 UTC of the day before (08:00 Tokyo). Exit: minute 00:54 UTC (09:55)."""

    prev = d - timedelta(days=1)
    return _ns(prev, 22, 59), _ns(d, 0, 54)


def _in_windows(d: date) -> bool:
    return any(a <= d < b for a, b in WINDOWS)


def _index(path: PricePath) -> dict[int, int]:
    t = path.columns["t_ns"]
    return {int(x): i for i, x in enumerate(t)}


def collect(paths: list[PricePath]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One row per candidate day (gotobi and control) with fills or the exclusion reason."""

    rows: list[dict[str, Any]] = []
    for p in paths:
        if p.symbol != "USDJPY":
            raise GotobiError(f"path {p.path_id} is {p.symbol}, not USDJPY")
        start = datetime.fromtimestamp(p.coverage_start_ns / _NS, UTC).date()
        end = datetime.fromtimestamp(p.coverage_end_ns / _NS, UTC).date()
        if not any(start >= a and end <= b for a, b in WINDOWS):
            raise GotobiError(f"path {p.path_id} covers {start}..{end}, outside the windows")
        idx, c = _index(p), p.columns
        g = set(gotobi_days(start, end))
        for d in sorted(g | set(control_days(start, end))):
            if not _in_windows(d):
                continue
            t0, t1 = window_minutes(d)
            row: dict[str, Any] = {"day": d.isoformat(), "gotobi": d in g, "path": p.path_id}
            i0, i1 = idx.get(t0), idx.get(t1)
            if i0 is None or i1 is None:
                row["excluded"] = "missing_minute"
            elif int(c["status"][i0]) != MinuteStatus.VALID:
                row["excluded"] = "flat_entry_minute"
            else:
                ask_in, bid_in = float(c["ask_close"][i0]), float(c["bid_close"][i0])
                ask_out, bid_out = float(c["ask_close"][i1]), float(c["bid_close"][i1])
                row |= {
                    "ask_in": ask_in,
                    "bid_in": bid_in,
                    "ask_out": ask_out,
                    "bid_out": bid_out,
                    "spread_in_pips": (ask_in - bid_in) / PIP,
                    "spread_out_pips": (ask_out - bid_out) / PIP,
                }
            rows.append(row)
    rows.sort(key=lambda r: r["day"])
    days = [r["day"] for r in rows]
    if len(days) != len(set(days)):
        raise GotobiError("the same day appears in two paths")
    return rows, data_check(rows)


def data_check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage and spreads only: no returns (frozen text §2)."""

    out: dict[str, Any] = {}
    for kind in (True, False):
        sel = [r for r in rows if r["gotobi"] is kind]
        ok = [r for r in sel if "excluded" not in r]
        reasons: dict[str, int] = {}
        for r in sel:
            if "excluded" in r:
                reasons[r["excluded"]] = reasons.get(r["excluded"], 0) + 1
        sp = [r["spread_in_pips"] + r["spread_out_pips"] for r in ok]
        out["gotobi" if kind else "control"] = {
            "candidates": len(sel),
            "valid": len(ok),
            "excluded": reasons,
            "median_spread_rt_pips": float(np.median(sp)) if sp else None,
            "zero_spread_sides": sum(
                (r["spread_in_pips"] <= 0) + (r["spread_out_pips"] <= 0) for r in ok
            ),
        }
    return out


def net_bp(r: dict[str, Any], cost_mult: float = 1.0, naga: bool = True) -> float:
    """Net return in bp of buying at the ASK and selling at the BID, Naga version by default."""

    mid_in = (r["ask_in"] + r["bid_in"]) / 2
    mid_out = (r["ask_out"] + r["bid_out"]) / 2
    spread_rt = (r["spread_in_pips"] + r["spread_out_pips"]) / 2  # half spread on each side
    if naga:
        spread_rt = max(spread_rt, NAGA_MIN_SPREAD_PIPS_RT)
    cost_pips = (spread_rt + 2 * SLIPPAGE_PIPS_PER_SIDE) * cost_mult
    gross = (mid_out - mid_in) / mid_in * 1e4
    return float(gross - cost_pips * PIP / mid_in * 1e4)


def _boot_ci(x: np.ndarray, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), (BOOT_N, len(x)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def assess(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "excluded" not in r]
    g = np.array([net_bp(r) for r in ok if r["gotobi"]])
    c = np.array([net_bp(r) for r in ok if not r["gotobi"]])
    if len(g) < 30 or len(c) < 30:
        raise GotobiError("too few valid days")
    g15 = np.array([net_bp(r, 1.5) for r in ok if r["gotobi"]])
    g2 = np.array([net_bp(r, 2.0) for r in ok if r["gotobi"]])
    graw = np.array([net_bp(r, naga=False) for r in ok if r["gotobi"]])
    lo, hi = _boot_ci(g, SEED_CI)
    rng = np.random.default_rng(SEED_CI + 1)
    diffs = g[rng.integers(0, len(g), (BOOT_N, len(g)))].mean(1) - c[
        rng.integers(0, len(c), (BOOT_N, len(c)))
    ].mean(1)
    rng = np.random.default_rng(SEED_RANDOM_DAYS)
    rand = np.array([c[rng.choice(len(c), len(g), replace=False)].mean() for _ in range(BOOT_N)])
    p95 = float(np.percentile(rand, 95))
    mean, diff = float(g.mean()), float(g.mean() - c.mean())
    cond = {
        "mean_net_naga_gt_0": mean > 0,
        "ci_above_0": lo > 0,
        "above_p95_random_days": mean > p95,
        "mean_net_x1_5_gt_0": float(g15.mean()) > 0,
        "diff_gotobi_minus_control_gt_0": diff > 0,
    }
    if all(cond.values()):
        outcome = "PASSED"
    elif cond["mean_net_naga_gt_0"] and cond["above_p95_random_days"] and not cond["ci_above_0"]:
        outcome = "INCONCLUSIVE"
    else:
        outcome = "REJECTED"
    by_year: dict[str, list[float]] = {}
    by_type: dict[str, list[float]] = {}
    for r, v in zip([r for r in ok if r["gotobi"]], g, strict=True):
        by_year.setdefault(r["day"][:4], []).append(float(v))
        d = date.fromisoformat(r["day"])
        by_type.setdefault(str(d.day), []).append(float(v))
    return {
        "outcome": outcome,
        "conditions": cond,
        "gotobi": {
            "n": len(g),
            "mean_net_bp": mean,
            "ci95": [lo, hi],
            "sd": float(g.std()),
            "win_rate": float((g > 0).mean()),
            "mean_x1_5": float(g15.mean()),
            "mean_x2": float(g2.mean()),
            "mean_dukascopy_spread_only": float(graw.mean()),
        },
        "control": {"n": len(c), "mean_net_bp": float(c.mean())},
        "diff_gotobi_minus_control": {
            "mean": diff,
            "ci95": [float(x) for x in np.percentile(diffs, [2.5, 97.5])],
        },
        "random_days_p95": p95,
        "by_year_mean_net_bp": {k: float(np.mean(v)) for k, v in sorted(by_year.items())},
        "by_calendar_day_mean_net_bp": {k: float(np.mean(v)) for k, v in sorted(by_type.items())},
    }


def run(prereg_file: Path, paths_root: Path, out_root: Path, check_only: bool) -> dict[str, Any]:
    pre = load_prereg(prereg_file)
    if not pre.paths:
        raise GotobiError("the TOML lists no paths: import them and add their ids first")
    paths = [load_path(paths_root, pid) for pid in pre.paths]
    rows, check = collect(paths)
    fp = hashlib.sha256(json.dumps([PROTOCOL, pre.sha256, list(pre.paths)]).encode()).hexdigest()
    out = out_root / fp[:16]
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "experiment_id": pre.experiment_id,
        "protocol": PROTOCOL,
        "prereg_sha256": pre.sha256,
        "frozen_text_sha256": pre.frozen_text_sha256,
        "paths": list(pre.paths),
        "data_check": check,
    }
    if check_only:
        (out / "data_check.json").write_text(json.dumps(report, indent=2))
        return report
    if (out / "report.json").exists():
        raise GotobiError(f"already run: {out / 'report.json'} (one run only, frozen text §8)")
    report["assessment"] = assess(rows)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    return report
