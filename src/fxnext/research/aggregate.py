"""Cross-symbol summary of experiments run with the same frozen baseline.

Each experiment stays as it was written (its directory is only read). The summary
puts the per-symbol numbers side by side and adds a pooled view in which trades
that are open at the same time on different symbols are identified, because they
are not independent events. Sums of R across symbols are descriptive only: they
are not a portfolio and imply no validated sizing.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from fxnext.research.metrics import by_year, summarize
from fxnext.research.protocol import ProtocolError

AGGREGATE_VERSION = "aggregate-v1"
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20261107
# +1 when a LONG on the symbol is long USD, -1 when it is short USD.
USD_SIGN = {"EURUSD": -1, "GBPUSD": -1, "USDJPY": 1}
_SAME = ("split", "interval", "provenance", "strategy", "versions_core", "exec_params", "costs")


def _load(result_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report = json.loads((result_dir / "report.json").read_text(encoding="utf-8"))
    rows = pq.read_table(result_dir / "outcomes.parquet").to_pylist()
    return report, rows


def _identity(report: dict[str, Any]) -> dict[str, Any]:
    v = report["versions"]
    return {
        "split": report["split"],
        "interval": report["interval"],
        "provenance": report["provenance"],
        "strategy": report.get("strategy"),
        "versions_core": {k: v[k] for k in ("research", "protocol", "exec_model", "cost_model")},
        "exec_params": report["inputs"]["exec_params"],
        "costs": report["inputs"]["costs"],
    }


def overlap_clusters(entry_ns: np.ndarray, exit_ns: np.ndarray) -> tuple[np.ndarray, int, float]:
    """Connected groups of trades whose [entry, exit] intervals overlap (any symbol).

    Returns (cluster index per trade, maximum number of trades open at once,
    time-weighted mean number open while at least one is open).
    """

    n = entry_ns.size
    cluster = np.zeros(n, dtype=np.int64)
    if n == 0:
        return cluster, 0, 0.0
    order = np.argsort(entry_ns, kind="stable")
    current, busy = -1, None
    for i in order.tolist():
        if busy is None or int(entry_ns[i]) > busy:
            current += 1
            busy = int(exit_ns[i])
        else:
            busy = max(busy, int(exit_ns[i]))
        cluster[i] = current
    events = sorted(
        [(int(t), 1) for t in entry_ns.tolist()] + [(int(t), -1) for t in exit_ns.tolist()],
        key=lambda e: (e[0], -e[1]),
    )
    open_now, peak, weighted, busy_time, last = 0, 0, 0.0, 0, None
    for t, step in events:
        if last is not None and open_now > 0:
            weighted += open_now * (t - last)
            busy_time += t - last
        open_now += step
        peak = max(peak, open_now)
        last = t
    return cluster, peak, (weighted / busy_time) if busy_time else 1.0


def aggregate(result_dirs: list[Path]) -> dict[str, Any]:
    if not result_dirs:
        raise ProtocolError("no experiment to aggregate")
    loaded = [_load(Path(d)) for d in result_dirs]
    ref = _identity(loaded[0][0])
    symbols: list[str] = []
    warnings: list[str] = []
    known = [r["strategy"] for r, _ in loaded if r.get("strategy") is not None]
    if known:
        ref["strategy"] = known[0]
    for report, _ in loaded:
        ident = _identity(report)
        if ident["strategy"] is None:
            # reports written before the strategy block existed (code 4bc112e)
            ident["strategy"] = ref["strategy"]
            warnings.append(
                f"{report['experiment_id']}: strategy parameters not recorded in its report; "
                "identity checked on versions, execution and costs only"
            )
        diff = [k for k in _SAME if ident[k] != ref[k]]
        if diff:
            raise ProtocolError(
                f"{report['experiment_id']} differs from {loaded[0][0]['experiment_id']} in {diff}"
            )
        symbols += list(report["per_symbol"])
    if len(set(symbols)) != len(symbols):
        raise ProtocolError("a symbol appears in more than one experiment")

    per_symbol: dict[str, Any] = {}
    trades: list[dict[str, Any]] = []
    for report, rows in loaded:
        for sym in report["per_symbol"]:
            per_symbol[sym] = {
                "experiment_id": report["experiment_id"],
                "fingerprint": report["fingerprint"],
                "code_version": report["versions"]["code_version"],
                "exec": report["streams"]["exec"] if len(report["per_symbol"]) == 1 else None,
                "exec_x1": report["per_symbol"][sym]["exec_x1"],
                "signal": report["per_symbol"][sym]["signal"],
                "by_year_r": report["per_symbol"][sym]["by_year_r"],
                "benchmarks": report["benchmarks"] if len(report["per_symbol"]) == 1 else None,
                "coverage": report["coverage"][sym],
                "reconciliation": (report.get("reconciliation") or {}).get(sym),
            }
        trades += [
            r
            for r in rows
            if r["stream"] == "exec" and r["variant"] == "x1" and r["r_net"] is not None
        ]

    trades.sort(key=lambda r: (int(r["entry_ns"]), r["symbol"], r["key"]))
    r = np.array([t["r_net"] for t in trades], dtype=np.float64)
    entry = np.array([t["entry_ns"] for t in trades], dtype=np.int64)
    exit_ = np.array([t["exit_ns"] for t in trades], dtype=np.int64)
    sym = [t["symbol"] for t in trades]
    start, end = ref["interval"]["start"], ref["interval"]["end"]
    months = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / (
        365.25 / 12 * 86_400
    )
    pooled = summarize(
        r, entry, exit_, months=months, bootstrap_samples=BOOTSTRAP_SAMPLES, seed=BOOTSTRAP_SEED
    )
    cluster, peak, mean_open = overlap_clusters(entry, exit_)
    sizes = Counter(cluster.tolist())
    multi = {c for c, k in sizes.items() if k > 1}
    cross = {c for c in multi if len({sym[i] for i in np.flatnonzero(cluster == c)}) > 1}
    in_cross = np.array([c in cross for c in cluster.tolist()], dtype=bool)
    cluster_mean = np.array(
        [float(r[cluster == c].mean()) for c in sorted(sizes)], dtype=np.float64
    )
    # pairs of trades on different symbols open at the same time, by USD exposure
    same_usd = opposite_usd = 0
    order = np.argsort(entry, kind="stable").tolist()
    for a_pos, a in enumerate(order):
        for b in order[a_pos + 1 :]:
            if int(entry[b]) > int(exit_[a]):
                break
            if sym[a] == sym[b]:
                continue
            ua = USD_SIGN.get(sym[a], 0) * (1 if trades[a]["direction"] == "LONG" else -1)
            ub = USD_SIGN.get(sym[b], 0) * (1 if trades[b]["direction"] == "LONG" else -1)
            if ua and ub:
                if ua == ub:
                    same_usd += 1
                else:
                    opposite_usd += 1
    return {
        "aggregate_version": AGGREGATE_VERSION,
        "note": (
            "Pooled numbers describe the set of trades; the sum of R is not a portfolio "
            "and implies no validated position sizing."
        ),
        "warnings": warnings,
        "split": ref["split"],
        "interval": ref["interval"],
        "provenance": ref["provenance"],
        "strategy": ref["strategy"],
        "experiments": [
            {"experiment_id": rep["experiment_id"], "fingerprint": rep["fingerprint"]}
            for rep, _ in loaded
        ],
        "per_symbol": per_symbol,
        "pooled": {
            "exec_x1": pooled.as_dict(),
            "bootstrap": {"samples": BOOTSTRAP_SAMPLES, "seed": BOOTSTRAP_SEED, "block": "week"},
            "by_year_r_sum": by_year(r, entry),
            "overlap": {
                "trades": int(r.size),
                "clusters": len(sizes),
                "clusters_with_more_trades": len(multi),
                "clusters_across_symbols": len(cross),
                "trades_overlapping_another_symbol": int(in_cross.sum()),
                "max_open_at_once": peak,
                "mean_open_while_any_open": mean_open,
                "cluster_mean_expectancy": (
                    float(cluster_mean.mean()) if cluster_mean.size else None
                ),
                "pairs_same_usd_direction": same_usd,
                "pairs_opposite_usd_direction": opposite_usd,
                "expectancy_isolated_trades": float(r[~in_cross].mean())
                if (~in_cross).any()
                else None,
                "expectancy_overlapping_trades": float(r[in_cross].mean())
                if in_cross.any()
                else None,
            },
        },
    }


def _f(v: Any, d: int = 3) -> str:
    if v is None:
        return "n/d"
    if isinstance(v, float):
        return f"{v:.{d}f}"
    return str(v)


def render_summary(s: dict[str, Any]) -> str:
    lines = [
        f"# Riepilogo baseline — split {s['split']}",
        "",
        f"- Intervallo: {s['interval']['start']} → {s['interval']['end']}",
        f"- Provenienza dati: {s['provenance']}",
        f"- Strategia: {s['strategy']}",
        "- Esperimenti: "
        + ", ".join(f"{e['experiment_id']} (`{e['fingerprint'][:12]}`)" for e in s["experiments"]),
        "",
        *[f"> Attenzione: {w}" for w in s["warnings"]],
        "",
        "Rendimenti MODELLED, in R. La somma degli R tra simboli è descrittiva: non è un "
        "portafoglio e non implica un sizing validato.",
        "",
        "## Per simbolo (exec ×1 netto, signal = lordo senza costi)",
        "",
        "| Simbolo | Trade | Netto | IC 95% | ×1,5 | ×2 | Lordo (signal) | IC lordo | PF | "
        "Max DD | Tot R | %ile dir. casuale | %ile tempo casuale |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sym, p in s["per_symbol"].items():
        x, g = p["exec_x1"], p["signal"]
        ex = p["exec"] or {}
        b = p["benchmarks"] or {}
        lines.append(
            f"| {sym} | {x['trades']} | {_f(x['expectancy'])} | "
            f"[{_f(x['ci_low'])}, {_f(x['ci_high'])}] | "
            f"{_f((ex.get('x1.5') or {}).get('expectancy'))} | "
            f"{_f((ex.get('x2') or {}).get('expectancy'))} | {_f(g['expectancy'])} | "
            f"[{_f(g['ci_low'])}, {_f(g['ci_high'])}] | {_f(x['profit_factor'], 2)} | "
            f"{_f(x['max_drawdown_r'], 1)} | {_f(x['total_r'], 1)} | "
            f"{_f((b.get('random_direction') or {}).get('baseline_percentile'), 1)} | "
            f"{_f((b.get('random_time') or {}).get('baseline_percentile'), 1)} |"
        )
    years = sorted({int(y) for p in s["per_symbol"].values() for y in p["by_year_r"]})
    lines += [
        "",
        "## R per anno (exec ×1)",
        "",
        "| Simbolo | " + " | ".join(str(y) for y in years) + " |",
        "|---|" + "---|" * len(years),
    ]
    for sym, p in s["per_symbol"].items():
        yr = {int(k): v for k, v in p["by_year_r"].items()}
        lines.append(f"| {sym} | " + " | ".join(_f(yr.get(y), 1) for y in years) + " |")
    pooled_years = {int(k): v for k, v in s["pooled"]["by_year_r_sum"].items()}
    lines.append(
        "| somma (descrittiva) | " + " | ".join(_f(pooled_years.get(y), 1) for y in years) + " |"
    )
    lines += ["", "## Copertura e riconciliazione", ""]
    for sym, p in s["per_symbol"].items():
        c, rec = p["coverage"], p["reconciliation"] or {}
        lines.append(
            f"- {sym}: {c['evaluations']} valutazioni, {c['candidates']} candidati, "
            f"buchi {c['path_gap']}, ambigui {c['ambiguous']}, fine dati {c['end_of_data']}, "
            f"bilanciato {'sì' if rec.get('balanced') else 'n/d'}"
        )
    pooled, ov = s["pooled"]["exec_x1"], s["pooled"]["overlap"]
    lines += [
        "",
        "## Vista aggregata (exec ×1)",
        "",
        f"- Trade: {pooled['trades']}, expectancy {_f(pooled['expectancy'])} R, "
        f"IC 95% [{_f(pooled['ci_low'])}, {_f(pooled['ci_high'])}] "
        "(bootstrap a blocchi settimanali: le settimane tengono insieme i trade "
        "contemporanei sui diversi cambi).",
        f"- Gruppi di trade sovrapposti nel tempo (qualsiasi simbolo): {ov['clusters']}, "
        f"di cui {ov['clusters_across_symbols']} con più cambi. Trade sovrapposti a un altro "
        f"cambio: {ov['trades_overlapping_another_symbol']} su {ov['trades']}.",
        f"- Massimo di trade aperti insieme: {ov['max_open_at_once']}; in media "
        f"{_f(ov['mean_open_while_any_open'], 2)} quando almeno uno è aperto.",
        f"- Coppie di trade contemporanei con la stessa esposizione al dollaro: "
        f"{ov['pairs_same_usd_direction']}; opposta: {ov['pairs_opposite_usd_direction']}.",
        f"- Expectancy dei trade isolati {_f(ov['expectancy_isolated_trades'])}, di quelli "
        f"sovrapposti {_f(ov['expectancy_overlapping_trades'])}, media per gruppo "
        f"{_f(ov['cluster_mean_expectancy'])}.",
        "",
    ]
    return "\n".join(lines)


def write_summary(result_dirs: list[Path], out_dir: Path) -> dict[str, Any]:
    s = aggregate(result_dirs)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(s, indent=2, sort_keys=True, default=str))
    (out_dir / "summary.md").write_text(render_summary(s), encoding="utf-8")
    return s


__all__ = ["aggregate", "overlap_clusters", "render_summary", "write_summary"]
