# ruff: noqa: E501
"""Markdown report of a g10 run (Italian, like the other research reports)."""

from __future__ import annotations

from typing import Any


def _pct(x: float | None, digits: int = 2) -> str:
    return "n/d" if x is None else f"{x * 100:+.{digits}f}%"


def _num(x: float | None, digits: int = 2) -> str:
    return "n/d" if x is None else f"{x:.{digits}f}"


def render_g10(r: dict[str, Any]) -> str:
    a = r["assessment"]
    q = r["data_check"]
    out = [
        f"# {r['experiment_id']}: carry e momentum G10, split {r['split']}",
        "",
        f"Fingerprint `{r['fingerprint']}`; pre-registrazione sha256 `{r['prereg_sha256'][:16]}`;"
        f" testo congelato sha256 `{r['frozen_text_sha256'][:16]}`.",
        f"Periodo {r['split_bounds'][0]} → {r['split_bounds'][1]}, {r['periods']} periodi mensili.",
        "",
        "## 1. Controllo dei dati (prima degli esiti)",
        "",
        "File annuali: " + ", ".join(f"{k} {v}" for k, v in sorted(q["files"].items())) + ".",
        f"Mesi senza giorno di ribilanciamento: {len(q['months_without_rebalance'])}"
        + (
            f" ({', '.join(q['months_without_rebalance'])})"
            if q["months_without_rebalance"]
            else ""
        )
        + ".",
        f"Ribilanciamenti non sull'ultimo giorno feriale: {len(q['rebalance_not_last_weekday'])}.",
        "",
        "| Coppia | Giorni feriali | Validi | Mancanti | Volume zero | BID>ASK |",
        "|---|---|---|---|---|---|",
    ]
    for sym, info in q["pairs"].items():
        tot = {
            k: sum(y[k] for y in info["by_year"].values())
            for k in ("weekdays", "valid", "missing", "zero_volume", "crossed")
        }
        out.append(
            f"| {sym} | {tot['weekdays']} | {tot['valid']} | {tot['missing']} | {tot['zero_volume']} | {tot['crossed']} |"
        )
    filled = r.get("zero_spread_filled") or {}
    if filled:
        out += [
            "",
            "Spread a zero sostituiti dalla mediana non nulla della coppia (costi v2): "
            + ", ".join(f"{c} {n}" for c, n in filled.items())
            + ".",
        ]
    out += [
        "",
        "Spread mediano a fine mese (pip), per anno:",
        "",
        "| Coppia | "
        + " | ".join(next(iter(q["month_end_spread_pips_median"].values())).keys())
        + " |",
    ]
    out.append("|---" * (1 + len(next(iter(q["month_end_spread_pips_median"].values())))) + "|")
    for sym, by_year in q["month_end_spread_pips_median"].items():
        out.append(f"| {sym} | " + " | ".join(f"{v:.1f}" for v in by_year.values()) + " |")
    out += ["", "Tassi, mesi mancanti nella finestra:", ""]
    for ccy, info in q["rates"].items():
        miss = info["missing_months"]
        out.append(
            f"- {ccy} ({info['series']}): {len(miss)}"
            + (f" ({', '.join(miss[:12])}{'…' if len(miss) > 12 else ''})" if miss else "")
        )
    out += ["", "## 2. Esito", "", f"**{a['outcome']}**", ""]
    if "best_variant" in a:
        out.append(f"Migliore variante: {a['best_variant']}.")
    for k, v in a.get("checks", {}).items():
        out.append(f"- {k}: {'sì' if v else 'no'}")
    if "max_of_k_p95" in a:
        out.append(
            f"- 95° percentile del massimo casuale su {r['max_of_k']['k']} varianti: {_pct(a['max_of_k_p95'], 3)} al mese"
        )
    out += [
        "",
        "## 3. Varianti (netto con costi ×1, mensile)",
        "",
        "| Variante | Mesi | Media | IC 95% | Sharpe ann. | DD max | Senza ricarico | Lordo | ×1,5 | ×2 | Pct casuale | Rotazione | Costo |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, v in r["variants"].items():
        n = v["net"]
        st = v["stress"]
        out.append(
            f"| {name} | {n['months']} | {_pct(n['mean'], 3)} | [{_pct(n['ci_low'], 3)}; {_pct(n['ci_high'], 3)}] | {_num(n['sharpe'])} | {n['max_drawdown'] * 100:.1f}% | "
            f"{_pct(v['net_no_markup']['mean'], 3)} | {_pct(v['gross']['mean'], 3)} | {_pct(st.get('1.5', {}).get('mean'), 3)} | {_pct(st.get('2.0', {}).get('mean'), 3)} | "
            f"{_num(v['random']['percentile_of_variant'], 1)} | {_num(v['turnover_mean'])} | {_pct(v['cost_mean'], 3)} |"
        )
    out += [
        "",
        "Sottoperiodi (media netta mensile):",
        "",
        "| Variante | "
        + " | ".join(next(iter(r["variants"].values()))["net"]["subperiods"])
        + " |",
    ]
    out.append("|---" * (1 + len(next(iter(r["variants"].values()))["net"]["subperiods"])) + "|")
    for name, v in r["variants"].items():
        out.append(
            f"| {name} | "
            + " | ".join(_pct(s["mean"], 3) for s in v["net"]["subperiods"].values())
            + " |"
        )
    out += ["", "Per anno (somma dei rendimenti mensili netti):", ""]
    years = list(next(iter(r["variants"].values()))["net"]["by_year"])
    out.append("| Variante | " + " | ".join(years) + " |")
    out.append("|---" * (1 + len(years)) + "|")
    for name, v in r["variants"].items():
        by = v["net"]["by_year"]
        out.append(
            f"| {name} | " + " | ".join(_pct(by.get(y, {}).get("sum"), 1) for y in years) + " |"
        )
    ref = r["reference_long_all_vs_usd"]
    out += [
        "",
        "## 4. Riferimento: lungo tutte le valute contro USD",
        "",
        f"Netto {_pct(ref['net']['mean'], 3)} al mese (Sharpe {_num(ref['net']['sharpe'])}), lordo {_pct(ref['gross']['mean'], 3)}.",
        "",
        "## 5. Avvisi",
        "",
    ]
    out += [f"- {w}" for w in r["warnings"]] or ["- nessuno"]
    for name, v in r["variants"].items():
        if v["excluded_months"]:
            out.append(f"- {name}, mesi esclusi: {'; '.join(v['excluded_months'])}")
    return "\n".join(out) + "\n"
