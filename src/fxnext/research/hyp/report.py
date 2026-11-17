# ruff: noqa: E501
"""Markdown report of a hypothesis run (Italian, like the M2 reports)."""

from __future__ import annotations

from typing import Any


def _f(x: Any, nd: int = 3) -> str:
    if x is None:
        return "n/d"
    if isinstance(x, bool):
        return "sì" if x else "no"
    if isinstance(x, float):
        return f"{x:+.{nd}f}" if nd else f"{x:.0f}"
    return str(x)


def _pct(x: Any) -> str:
    return "n/d" if x is None else f"{100 * x:.1f}%"


def render_hypothesis(r: dict[str, Any]) -> str:
    out = [
        f"# {r['experiment_id']}",
        "",
        f"Ipotesi: {r['hypothesis']}",
        "",
        f"- Split: {r['split']} {r['interval']['start']} → {r['interval']['end']}",
        f"- Esplorativa: {_f(r['exploratory'])}",
        f"- Fingerprint: `{r['fingerprint']}`",
        f"- Pre-registrazione sha256: `{r['prereg_sha256']}`",
        f"- Testo congelato sha256: `{r['frozen_text_sha256']}`",
        f"- Codice: `{r['versions']['code_version']}`",
        "",
        "Percorsi M1 (BID+ASK):",
        "",
    ]
    for sym, pid in r["inputs"]["paths"].items():
        src = r["inputs"]["source_paths"][sym]
        ref = r["inputs"]["reference_reports"].get(sym, "")
        out.append(f"- {sym}: `{pid}` (sorgente `{src[:12]}…`, come {ref})")
    out.append("")
    if "s0" in r:
        out += _s0(r["s0"])
    if "phase1" in r:
        out += _phase1(r)
    if "variants" in r:
        out += _variants(r)
    if r.get("warnings"):
        out += ["## Avvertenze", ""] + [f"- {w}" for w in r["warnings"]] + [""]
    return "\n".join(out) + "\n"


def _s0(s0: dict[str, Any]) -> list[str]:
    out = [
        "## S0 · Variance ratio (descrittivo)",
        "",
        "Soglie: |z| > 2,58 e |VR − 1| ≥ 0,05. Le celle D1 sono solo indicative.",
        "",
        "| Coppia | Barra | Sessione | q | Rendimenti | VR | IC 95% | z | Lettura |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sym, tfs in s0.items():
        for tf, cells in tfs.items():
            for sess, qs in cells.items():
                if sess == "counts":
                    continue
                for q, c in qs.items():
                    if c.get("status") != "ok":
                        out.append(
                            f"| {sym} | {tf} | {sess} | {q[1:]} | {c['returns']} | – | – | – "
                            f"| {c.get('status')} |"
                        )
                        continue
                    out.append(
                        f"| {sym} | {tf} | {sess} | {q[1:]} | {c['returns']} | {c['vr']:.3f} "
                        f"| [{c['ci_low']:.3f}; {c['ci_high']:.3f}] | {c['z']:+.2f} | {c['label']} |"
                    )
    out += ["", "Esclusioni per coppia e barra:", ""]
    for sym, tfs in s0.items():
        for tf, cells in tfs.items():
            out.append(f"- {sym} {tf}: {cells['counts']}")
    return out + [""]


def _phase1(r: dict[str, Any]) -> list[str]:
    p = r["phase1"]
    out = ["## Fase 1 (descrittiva)", ""]
    if r["kind"] == "s7_intraday":
        out += [
            "| Coppia | Sessione | Giorni | Media log | IC 95% | Segno previsto | Gamba ok |",
            "|---|---|---|---|---|---|---|",
        ]
        for sym, sess in p["per_symbol"].items():
            for name, c in sess.items():
                if c.get("n", 0) < 2:
                    out.append(f"| {sym} | {name} | {c.get('n', 0)} | – | – | – | – |")
                    continue
                out.append(
                    f"| {sym} | {name} | {c['n']} | {c['mean'] * 1e4:+.2f} bp "
                    f"| [{c['ci_low'] * 1e4:+.2f}; {c['ci_high'] * 1e4:+.2f}] bp "
                    f"| {c.get('predicted_sign', '–')} | {_f(c.get('leg_passes'))} |"
                )
        out.append("")
        for leg, info in p["legs"].items():
            pooled = info["pooled_by_day"]
            extra = (
                f", media allineata {pooled['mean_aligned'] * 1e4:+.2f} bp "
                f"[{pooled['ci_low'] * 1e4:+.2f}; {pooled['ci_high'] * 1e4:+.2f}] (cluster per giorno)"
                if "mean_aligned" in pooled
                else ""
            )
            out.append(
                f"- Gamba {leg}: coppie ok {info['pairs_passing']} → passa {_f(info['passes'])}{extra}"
            )
        out += ["", f"Varianti ammesse alla fase 2: {p['phase2_variants'] or 'nessuna'}", ""]
        out.append(f"Sessioni escluse: {p['excluded']}")
    else:
        out.append(f"Controllo dati: {p['data_check']}")
        out += [
            "",
            "| k | Gruppo | Weekend | Chiusura osservata | Attesa random walk | Differenza IC 95% | Passa |",
            "|---|---|---|---|---|---|---|",
        ]
        for k, groups in p["by_k"].items():
            for g, c in groups.items():
                if not c.get("weekends"):
                    out.append(f"| {k[1:]} | {g} | 0 | – | – | – | no |")
                    continue
                out.append(
                    f"| {k[1:]} | {g} | {c['weekends']} | {_pct(c['observed_rate'])} "
                    f"| {_pct(c['expected_rw_rate'])} "
                    f"| [{100 * c['difference_ci_low']:+.1f}; {100 * c['difference_ci_high']:+.1f}] pp "
                    f"| {_f(c['passes'])} |"
                )
        out += [
            "",
            f"Soglie k ammesse alla fase 2 (decide il gruppo pooled): {p['phase2_k'] or 'nessuna'}",
        ]
    return out + [""]


def _variants(r: dict[str, Any]) -> list[str]:
    vs = r["variants"]
    out = ["## Varianti (tutte, non solo la migliore)", ""]
    if r.get("variants_not_run"):
        out += [f"Non eseguite (fase 1 non superata): {r['variants_not_run']}", ""]
    if not vs:
        return out
    syms = sorted({s for v in vs.values() for s in v["per_symbol"]})
    out += [
        "| Variante | Parametri | Trade | Netto ×1 R | IC 95% | ×1,5 | ×2 | Segnale R | Vincite | PF "
        "| DD R | Pip netti | Costo R | " + " | ".join(syms) + " | pct rnd dir | pct rnd time |",
        "|" + "---|" * (14 + len(syms)),
    ]
    for name, v in vs.items():
        e = v["streams"]["exec"]
        x1 = e["x1"]
        params = ", ".join(f"{k}={val}" for k, val in v["params"].items() if k != "name")
        b = v["benchmarks"]
        out.append(
            f"| {name} | {params} | {x1['trades']} | {_f(x1['expectancy'])} "
            f"| [{_f(x1['ci_low'])}; {_f(x1['ci_high'])}] | {_f(e.get('x1.5', {}).get('expectancy'))} "
            f"| {_f(e.get('x2', {}).get('expectancy'))} | {_f(v['streams']['signal']['expectancy'])} "
            f"| {_pct(x1['win_rate'])} | {_f(x1['profit_factor'], 2)} | {x1['max_drawdown_r']:.1f} "
            f"| {_f(v['units'].get('net_expectancy_pips'), 2)} | {_f(v['units'].get('mean_cost_r'))} | "
            + " | ".join(
                _f(v["per_symbol"].get(s, {}).get("exec_x1", {}).get("expectancy")) for s in syms
            )
            + f" | {_f(b.get('random_direction', {}).get('variant_percentile'), 1)} "
            f"| {_f(b.get('random_time', {}).get('variant_percentile'), 1)} |"
        )
    out.append("")
    if any(v.get("entry_test") for v in vs.values()):
        out += [
            "Test dell'ingresso (flusso segnale, lordo, tutti i candidati):",
            "",
            "| Variante | N barre: vincite | N barre: media R | 1 R simmetrico: vincite | 1 R: media R |",
            "|---|---|---|---|---|",
        ]
        for name, v in vs.items():
            t = v.get("entry_test") or {}
            nb, sy = t.get("nbar", {}), t.get("sym1r", {})
            out.append(
                f"| {name} | {_pct(nb.get('gross_win_rate'))} | {_f(nb.get('gross_mean_r'))} "
                f"| {_pct(sy.get('gross_win_rate'))} | {_f(sy.get('gross_mean_r'))} |"
            )
        out.append("")
    if any("swap" in v for v in vs.values()):
        out += [
            "Swap (S2): netto con e senza swap, notti tenute, ingressi nella stessa settimana:",
            "",
            "| Variante | Netto con swap | Netto senza swap | Swap medio R | Notti medie | Settimane con ingressi su 2+ coppie |",
            "|---|---|---|---|---|---|",
        ]
        for name, v in vs.items():
            s = v["swap"]
            out.append(
                f"| {name} | {_f(v['streams']['exec']['x1']['expectancy'])} "
                f"| {_f(s['net_without_swap']['expectancy'])} | {_f(s['mean_swap_r'])} "
                f"| {_f(s['mean_nights'], 1)} | {v['same_week_entries']['weeks_with_entries_on_2_or_more_pairs']} "
                f"di {v['same_week_entries']['weeks_with_entries']} |"
            )
        out.append("")
    out += ["Benchmark per variante:", ""]
    for name, v in vs.items():
        out.append(f"- {name}: {v['benchmarks']}")
    out += ["", "Riconciliazione (eseguiti + controfattuali + non entrati = candidati):", ""]
    for name, v in vs.items():
        for sym, s in v["per_symbol"].items():
            rc = s["reconciliation"]
            out.append(
                f"- {name} {sym}: candidati {rc['candidates']}, eseguiti {rc['exec_executed']}, "
                f"controfattuali {rc['exec_counterfactual']}, non entrati {rc['exec_not_entered']}, "
                f"fine dati {rc['exec_end_of_data']}, bilanciata {_f(rc['balanced'])}"
            )
    a = r["assessment"]
    out += ["", "## Soglia per proporre la validazione", "", f"Esito: **{a['outcome']}**", ""]
    if a.get("note"):
        out += [a["note"], ""]
    for k, val in (a.get("checks") or {}).items():
        out.append(f"- {k}: {_f(val)}")
    for k in (
        "judged_variants",
        "best_variant",
        "best_expectancy_x1",
        "positive_pairs",
        "positive_variants",
        "plateau_needed",
        "max_of_k",
        "entry_test_rule",
        "multiplicity_note",
    ):
        if k in a:
            out.append(f"- {k}: {a[k]}")
    return out + [""]
