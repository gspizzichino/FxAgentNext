"""Markdown rendering of an experiment report (numbers as computed, no interpretation)."""

from __future__ import annotations

from typing import Any


def _f(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/d"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _row(name: str, s: dict[str, Any] | None) -> str:
    if not s:
        return f"| {name} | n/d | | | | | | |"
    ci = f"[{_f(s['ci_low'])}, {_f(s['ci_high'])}]"
    return (
        f"| {name} | {s['trades']} | {s['independent']} | {_f(s['expectancy'])} | {ci} | "
        f"{_f(s['profit_factor'], 2)} | {_f(s['max_drawdown_r'], 1)} | {_f(s['total_r'], 1)} |"
    )


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Esperimento {report['experiment_id']} — split {report['split']}",
        "",
        f"Ipotesi: {report['hypothesis']}",
        "",
        f"- Intervallo: {report['interval']['start']} → {report['interval']['end']}",
        f"- Provenienza dati: {report['provenance']}",
        f"- Fingerprint: `{report['fingerprint']}`",
        f"- Pre-registrazione sha256: `{report['prereg_sha256']}`",
        f"- Codice: `{report['versions']['code_version']}`",
        "",
        "Tutti i rendimenti sono MODELLED, in R (R = 1,5 × ATR14 alla decisione).",
        "",
    ]
    for w in report["warnings"]:
        lines.append(f"> Attenzione: {w}")
    if report["warnings"]:
        lines.append("")
    lines += [
        "## Rendimenti",
        "",
        "| Stream | Trade | Indip. | Expectancy | IC 95% | PF | Max DD | Totale R |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, s in report["streams"]["exec"].items():
        lines.append(_row(f"exec {name}", s))
    lines.append(_row("exec, target per primo", report["streams"]["exec_target_first"]))
    lines.append(_row("signal (senza costi)", report["streams"]["signal"]))
    lines.append(_row("counterfactual", report["streams"]["counterfactual"]))
    lines.append("| broker | vuoto fino a M3 | | | | | | |")
    b = report["benchmarks"]
    lines += [
        "",
        "## Benchmark nulli (exec ×1)",
        "",
        "| Benchmark | p05 | mediana | p95 | Percentile baseline |",
        "|---|---|---|---|---|",
    ]
    for name in ("random_direction", "random_time"):
        d = b[name]
        if d.get("repetitions"):
            lines.append(
                f"| {name} | {_f(d['p05'])} | {_f(d['median'])} | {_f(d['p95'])} | "
                f"{_f(d['baseline_percentile'], 1)} |"
            )
    lines += [
        f"| always_long | {_f(b['always_long'])} | | | |",
        f"| always_short | {_f(b['always_short'])} | | | |",
        "",
        "## R per anno (exec ×1)",
        "",
        "| Anno | R |",
        "|---|---|",
    ]
    lines += [f"| {y} | {_f(v, 1)} |" for y, v in sorted(report["by_year_r"].items())]
    lines += ["", "## Copertura", ""]
    for sym, c in report["coverage"].items():
        lines.append(
            f"- {sym}: {c['evaluations']} valutazioni, {c['candidates']} candidati, "
            f"esiti {c['exec_reasons']}, ambigui {c['ambiguous']}, con buchi {c['path_gap']}, "
            f"non eseguiti {c['not_filled']}, fine dati {c['end_of_data']}"
        )
    rec = report.get("reconciliation") or {}
    if rec:
        lines += [
            "",
            "## Riconciliazione candidati ed esiti (exec ×1)",
            "",
            "| Simbolo | Candidati | Eseguiti | di cui fine dati | Counterfactual | Non entrati | "
            "Scadono oltre fine split | Entrate/uscite fuori split | Bilanciato |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for sym, r in rec.items():
            outside = r["entries_outside_split"] + r["exits_outside_split"]
            lines.append(
                f"| {sym} | {r['candidates']} | {r['exec_executed']} | {r['exec_end_of_data']} | "
                f"{r['exec_counterfactual']} | {r['exec_not_entered']} | "
                f"{r['candidates_expiring_after_end']} | {outside} | "
                f"{'sì' if r['balanced'] else 'no'} |"
            )
    crit = report["criteria"]
    lines += ["", "## Esito secondo i criteri pre-registrati", "", f"**{crit['outcome']}**", ""]
    for k, v in crit.get("checks", {}).items():
        lines.append(f"- {k}: {'sì' if v else 'no'}")
    lines.append("")
    return "\n".join(lines)
