"""FxAgent Next web app: research screens and supervision of the demo engine.

The app reads files that other commands wrote. It never imports the replay,
the downloader, the experiment runner or the engine. Its only write is one POST
route that appends a command (kill switch, resume) to the engine's queue; the
engine applies it and records the result. No route opens or closes a trade.
Both rules are checked by tests and import-linter.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fxnext.dashboard import charts
from fxnext.dashboard import readers as rd
from fxnext.dashboard.news_view import news_page
from fxnext.dashboard.telegram import TelegramConfig
from fxnext.news.archive import archive_status
from fxnext.ops.store import (
    CMD_CLEAR_SUSPENSION,
    CMD_KILL_OFF,
    CMD_KILL_ON,
    CMD_RESUME_WEEKLY,
    enqueue_command,
)
from fxnext.research.protocol import SPLITS

HERE = Path(__file__).parent
REFRESH_SECONDS = 30  # pages that follow something in progress (download, the engine)
# The only commands the app can queue. None of them opens or closes a trade.
DEMO_COMMANDS = frozenset({CMD_KILL_ON, CMD_KILL_OFF, CMD_RESUME_WEEKLY, CMD_CLEAR_SUSPENSION})

NAV = [
    (
        "Ogni giorno",
        [
            ("oggi", "Oggi (demo)", ""),
            ("posizioni", "Posizioni e trade", ""),
            ("segnali", "Segnali e decisioni", ""),
            ("proposte", "Proposte AI", "M4"),
            ("news", "News e sentiment", ""),
        ],
    ),
    (
        "Ricerca",
        [
            ("stato", "Stato della ricerca", ""),
            ("esperimenti", "Esperimenti", ""),
            ("confronto", "Confronto coppie", ""),
            ("preregistrazioni", "Pre-registrazioni", ""),
        ],
    ),
    ("Sistema", [("dati", "Dati", ""), ("sistema", "Motore e avvisi", "")]),
]
LATER = {
    "proposte": (
        "Proposte AI",
        "M4",
        "Le ipotesi proposte dall'agente di ricerca. Approvare crea "
        "una bozza di pre-registrazione, mai una configurazione attiva. Arriva con M4.",
    ),
}


def _fmt_int(v: Any) -> str:
    return f"{v:,}".replace(",", ".") if isinstance(v, int) else "–"


def _fmt_ci(ci: Any) -> str:
    lo, hi = ci if ci else (None, None)
    if lo is None or hi is None:
        return "–"
    return f"[{charts.fmt_r(lo)}; {charts.fmt_r(hi)}]"


def _cls(v: Any) -> str:
    if not isinstance(v, int | float):
        return ""
    return "pos" if v > 0 else "neg" if v < 0 else ""


def _num(v: Any, digits: int = 2) -> str:
    if not isinstance(v, int | float):
        return "–"
    return f"{v:.{digits}f}".replace(".", ",")


def _whole(v: Any, signed: bool = False) -> str:
    if not isinstance(v, int | float):
        return "–"
    return f"{v:{'+' if signed else ''},.0f}".replace(",", ".")


def create_app(sources: rd.Sources, *, today: date | None = None) -> FastAPI:
    app = FastAPI(title="FxAgent Next", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(r=charts.fmt_r, ci=_fmt_ci, cls=_cls, int=_fmt_int, num=_num, whole=_whole)
    env.globals.update(nav=NAV)

    def render(request: Request, name: str, page: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"page": page, **ctx})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/stato", status_code=307)

    @app.get("/stato", response_class=HTMLResponse)
    def stato(request: Request) -> HTMLResponse:
        experiments = rd.list_experiments(sources.research)
        primary = [e for e in experiments if not e["group"]]
        preregs = rd.list_preregistrations(sources.prereg, experiments)
        summaries = rd.list_summaries(sources.research)
        return render(
            request,
            "stato.html",
            "stato",
            experiments=primary,
            preregs=preregs,
            changed=[p for p in preregs if p["status"] == "CHANGED"],
            summary=summaries[0] if summaries else None,
            holdout_end=SPLITS["holdout"][1].date().isoformat(),
        )

    @app.get("/esperimenti", response_class=HTMLResponse)
    def esperimenti(request: Request) -> HTMLResponse:
        return render(
            request,
            "esperimenti.html",
            "esperimenti",
            experiments=rd.list_experiments(sources.research),
            summaries=rd.list_summaries(sources.research),
        )

    @app.get("/esperimenti/{fingerprint}", response_class=HTMLResponse)
    def esperimento(request: Request, fingerprint: str) -> HTMLResponse:
        found = rd.find_experiment(sources.research, fingerprint)
        if found is None:
            raise HTTPException(404, "Esperimento non trovato")
        result_dir, report = found
        curve = rd.equity_curve(result_dir)
        exec_x1 = report["streams"]["exec"].get("x1") or {}
        bench = report.get("benchmarks") or {}
        streams = [
            (f"exec ×{k[1:].replace('.', ',')}", v) for k, v in report["streams"]["exec"].items()
        ]
        streams += [
            ("exec, target per primo", report["streams"].get("exec_target_first")),
            ("signal (senza costi)", report["streams"].get("signal")),
            ("counterfactual", report["streams"].get("counterfactual")),
        ]
        years = sorted((report.get("by_year_r") or {}).items())
        return render(
            request,
            "esperimento.html",
            "esperimenti",
            r=report,
            streams=[(n, s) for n, s in streams if s],
            equity=charts.equity(curve["points"]),
            reasons=curve.get("reasons", {}),
            bench_chart=(
                charts.benchmarks(bench, float(exec_x1["expectancy"]), "Benchmark nulli")
                if bench and exec_x1.get("expectancy") is not None
                else None
            ),
            bench=bench,
            years=years,
            year_chart=charts.grouped_bars(
                [y for y, _ in years], [("R", [float(v) for _, v in years])], "R per anno"
            ),
            trades=rd.recent_trades(result_dir, 30),
            ts=lambda ns: (
                datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M")
                if isinstance(ns, int)
                else "–"
            ),
        )

    @app.get("/confronto", response_class=HTMLResponse)
    def confronto_index(request: Request) -> HTMLResponse:
        summaries = rd.list_summaries(sources.research)
        if summaries:
            return confronto(request, summaries[0]["_name"])
        return render(
            request,
            "vuoto.html",
            "confronto",
            title="Confronto coppie",
            text="Nessun riepilogo trovato. Si crea con fxnext research-summary.",
        )

    @app.get("/confronto/{name:path}", response_class=HTMLResponse)
    def confronto(request: Request, name: str) -> HTMLResponse:
        s = rd.find_summary(sources.research, name)
        if s is None:
            raise HTTPException(404, "Riepilogo non trovato")
        per = s.get("per_symbol") or {}
        rows: list[dict[str, object]] = []
        for sym, d in sorted(per.items()):
            e, g = d.get("exec_x1") or {}, d.get("signal") or {}
            rows.append(
                {
                    "label": f"{sym} netto",
                    "value": e.get("expectancy", 0.0),
                    "lo": e.get("ci_low"),
                    "hi": e.get("ci_high"),
                    "filled": True,
                }
            )
            rows.append(
                {
                    "label": f"{sym} lordo",
                    "value": g.get("expectancy", 0.0),
                    "lo": g.get("ci_low"),
                    "hi": g.get("ci_high"),
                    "filled": False,
                }
            )
        pooled = (s.get("pooled") or {}).get("exec_x1") or {}
        if pooled:
            rows.append(
                {
                    "label": "Aggregato netto",
                    "value": pooled.get("expectancy", 0.0),
                    "lo": pooled.get("ci_low"),
                    "hi": pooled.get("ci_high"),
                    "filled": True,
                    "bold": True,
                }
            )
        years = sorted({y for d in per.values() for y in (d.get("by_year_r") or {})})
        series = [
            (sym, [float((d.get("by_year_r") or {}).get(y, 0.0)) for y in years])
            for sym, d in sorted(per.items())
        ]
        return render(
            request,
            "confronto.html",
            "confronto",
            s=s,
            per=sorted(per.items()),
            forest=charts.forest(rows, "Expectancy con intervallo di confidenza"),
            bars=charts.grouped_bars(years, series, "R per anno e per coppia"),
            overlap=(s.get("pooled") or {}).get("overlap") or {},
            pooled=pooled,
            summaries=rd.list_summaries(sources.research),
        )

    @app.get("/preregistrazioni", response_class=HTMLResponse)
    def preregistrazioni(request: Request) -> HTMLResponse:
        experiments = rd.list_experiments(sources.research)
        return render(
            request,
            "preregistrazioni.html",
            "preregistrazioni",
            preregs=rd.list_preregistrations(sources.prereg, experiments),
            frozen=rd.frozen_documents(sources.frozen),
        )

    @app.get("/dati", response_class=HTMLResponse)
    def dati(request: Request) -> HTMLResponse:
        cov = rd.data_coverage(sources.raw)
        now = today or datetime.now(tz=UTC).date()
        return render(
            request,
            "dati.html",
            "dati",
            cov=cov,
            expected={y: rd.expected_days(y, now) for y in cov["years"]},
            split_of_year={y: rd.split_of_day(date(y, 7, 1)) for y in cov["years"]},
            datasets=rd.list_datasets(sources.datasets),
            refresh=REFRESH_SECONDS,
            news=archive_status(sources.news),
        )

    @app.get("/news", response_class=HTMLResponse)
    def news(request: Request) -> HTMLResponse:
        return render(
            request, "news.html", "news", news=news_page(sources.news), refresh=REFRESH_SECONDS
        )

    @app.get("/sistema", response_class=HTMLResponse)
    def sistema(request: Request) -> HTMLResponse:
        return render(request, "sistema.html", "sistema", telegram=TelegramConfig.from_env())

    def demo(request: Request, name: str, page: str) -> HTMLResponse:
        view = rd.demo_view(sources.ops, datetime.now(UTC))
        return render(request, name, page, demo=view, refresh=REFRESH_SECONDS, ops=sources.ops)

    @app.get("/oggi", response_class=HTMLResponse)
    def oggi(request: Request) -> HTMLResponse:
        return demo(request, "oggi.html", "oggi")

    @app.get("/posizioni", response_class=HTMLResponse)
    def posizioni(request: Request) -> HTMLResponse:
        return demo(request, "posizioni.html", "posizioni")

    @app.get("/segnali", response_class=HTMLResponse)
    def segnali(request: Request) -> HTMLResponse:
        return demo(request, "segnali.html", "segnali")

    @app.post("/oggi/comando")
    async def comando(request: Request) -> RedirectResponse:
        # urlencoded form parsed by hand: no extra dependency for one route
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        command = (form.get("command") or [""])[0]
        confirm = (form.get("confirm") or [""])[0]
        if command not in DEMO_COMMANDS:
            raise HTTPException(400, "Comando non ammesso")
        if confirm != "si":
            raise HTTPException(400, "Serve la conferma")
        if not sources.ops.exists():
            raise HTTPException(409, "Il motore non è mai partito")
        enqueue_command(
            sources.ops,
            command,
            "dashboard",
            int(datetime.now(UTC).timestamp() * 1e6) * 1000,
        )
        return RedirectResponse("/oggi", status_code=303)

    @app.get("/{section}", response_class=HTMLResponse)
    def later(request: Request, section: str) -> HTMLResponse:
        if section not in LATER:
            raise HTTPException(404, "Pagina non trovata")
        title, milestone, text = LATER[section]
        return render(request, "vuoto.html", section, title=title, milestone=milestone, text=text)

    return app


__all__ = ["create_app"]
