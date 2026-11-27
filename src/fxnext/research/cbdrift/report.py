# ruff: noqa: E501
"""Markdown report of an h1-cbdrift-001 run (Italian, like the other research reports)."""

from __future__ import annotations

from typing import Any


def _bp(x: float | None) -> str:
    return "n/d" if x is None else f"{x * 1e4:+.1f} bp"


def _num(x: float | None, digits: int = 2) -> str:
    return "n/d" if x is None else f"{x:.{digits}f}"


def _row(name: str, s: dict[str, Any]) -> str:
    return f"| {name} | {s['events']} | {_bp(s['mean'])} | {_num(s['t'])} | {_num(s['positive_share'])} |"


def render_cbdrift(r: dict[str, Any]) -> str:
    a = r["assessment"]
    q = r["data_check"]
    c = r["counts"]
    out = [
        f"# {r['experiment_id']}: deriva prima delle decisioni delle banche centrali, split {r['split']}",
        "",
        f"Fingerprint `{r['fingerprint']}`; TOML sha256 `{r['prereg_sha256'][:16]}`;"
        f" testo congelato sha256 `{r['frozen_text_sha256'][:16]}`; calendario sha256 `{q['calendar_sha256'][:16]}`.",
        f"Periodo {r['split_bounds'][0]} → {r['split_bounds'][1]}.",
        "",
        "## 1. Controllo dei dati (prima degli esiti)",
        "",
        "Valute ammesse: " + ", ".join(q["admitted"]) + ".",
        "",
        "| Valuta | Serie 1 mese | Copertura | Overnight | Copertura | Ammessa |",
        "|---|---|---|---|---|---|",
    ]
    for ccy, i in q["admission"].items():
        cov = i["coverage"]
        out.append(
            f"| {ccy} | {i['one_month'] or '—'} | {_num(cov['one_month'])} | {i['overnight']} | {_num(cov['overnight'])} | {'sì' if i['admitted'] else 'no'} |"
        )
    out += [
        "",
        "| Coppia | Giorni feriali | Validi | Mancanti | Volume zero | BID>ASK | Spread zero |",
        "|---|---|---|---|---|---|---|",
    ]
    for sym, p in q["fx"]["pairs"].items():
        out.append(
            f"| {sym} | {p['weekdays']} | {p['valid']} | {p['missing']} | {p['zero_volume']} | {p['crossed']} | {p['zero_spread']} |"
        )
    out += [
        "",
        f"Annunci non programmati nello split: {len(q['unscheduled_in_split'])}.",
        "",
        "## 2. Eventi",
        "",
        f"Riunioni considerate {c['meetings_considered']}; nessuna posizione (|Δ| < 10 bp) {c['no_position']};"
        f" rialzi previsti {c['hikes_expected']}; tagli previsti {c['cuts_expected']};"
        f" spread a zero sostituiti {c['zero_spread_filled']}.",
        "Esclusi: "
        + (", ".join(f"{k} {v}" for k, v in sorted(c["excluded"].items())) or "nessuno")
        + ".",
        "",
        "## 3. Risultati",
        "",
        "| Serie | Eventi | Media per evento | t | Quota positiva |",
        "|---|---|---|---|---|",
        _row("Netto, costi ×1", r["net"]),
        _row("Lordo", r["gross"]),
        _row("Netto senza ricarico", r["net_no_markup"]),
    ]
    for m, s in r["stress"].items():
        out.append(_row(f"Netto, costi ×{m}", s))
    out.append(_row("Riferimento: sempre lunghi la valuta", r["reference_long_currency"]["net"]))
    n = r["net"]
    rd = r["random_direction"]
    out += [
        "",
        f"IC 95% della media netta (bootstrap per mese): [{_bp(n['ci_low'])}; {_bp(n['ci_high'])}].",
        f"Direzione casuale ({rd.get('repetitions', 0)} ripetizioni): 5° {_bp(rd.get('p5'))}, mediana {_bp(rd.get('p50'))}, 95° {_bp(rd.get('p95'))};"
        f" la regola sta al percentile {_num(rd.get('percentile_of_rule'), 1)}.",
        "",
        "Componenti medie: "
        + ", ".join(f"{k} {_bp(v)}" for k, v in r["components_mean"].items())
        + ".",
        "",
        "### Spaccature (riportate, non usate per decidere)",
        "",
        "| Gruppo | Eventi | Media per evento | t | Quota positiva |",
        "|---|---|---|---|---|",
    ]
    for k, s in r["by_direction"].items():
        out.append(_row(k, s))
    for k, s in r["subperiods"].items():
        out.append(_row(k, s))
    for k, s in r["by_bank"].items():
        out.append(_row(k, s))
    for k, s in r["by_year"].items():
        out.append(_row(k, s))
    out += ["", "## 4. Esito", "", f"**{a['outcome']}**", ""]
    for k, v in a.get("checks", {}).items():
        out.append(f"- {k}: {'sì' if v else 'no'}")
    out.append("")
    return "\n".join(out)
