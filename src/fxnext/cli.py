"""Command line: download, import, inspect, replay, research, verify, explain, dashboard."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fxnext.audit.explain import explain
from fxnext.audit.store import AuditDestination, read_manifest
from fxnext.data.dukascopy import RawCache, download_range
from fxnext.data.importers import cross_check_bid_ask, import_dukascopy
from fxnext.data.path import import_path_dukascopy, write_path
from fxnext.data.store import load_dataset, write_dataset
from fxnext.data.synthetic import synthetic_m15
from fxnext.domain.enums import DatasetProvenance, PriceBasis
from fxnext.replay.runner import run_replay
from fxnext.research.experiment import run_experiment, run_grid


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def code_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown-dirty"
    return f"{sha}-dirty" if dirty else sha


def _print_progress(record: dict[str, Any]) -> None:
    detail = f" ({record['detail']})" if record.get("detail") else ""
    print(
        f"{record['symbol']} {record['basis']} {record['day']} {record['status']} "
        f"tentativi={record['attempts']}{detail}",
        file=sys.stderr,
        flush=True,
    )


def _utf8_output() -> None:
    """On Windows a redirected stdout uses cp1252 and fails on characters of the
    central bank texts: write UTF-8 everywhere, replacing what cannot be written."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    parser = argparse.ArgumentParser(prog="fxnext")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("download", help="download Dukascopy M1 candles into the raw cache")
    p.add_argument("--cache", type=Path, default=Path("data/raw/dukascopy"))
    p.add_argument("--symbol", action="append", required=True)
    p.add_argument("--basis", action="append", choices=["BID", "ASK"])
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    p.add_argument("--min-interval", type=float, default=1.0, help="seconds between requests")

    p = sub.add_parser("import", help="build M15 datasets and the M1 path from the raw cache")
    p.add_argument("--cache", type=Path, default=Path("data/raw/dukascopy"))
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))
    p.add_argument("--paths", type=Path, default=Path("data/paths"))
    p.add_argument("--symbol", required=True)
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True)

    p = sub.add_parser("synthetic", help="write a deterministic synthetic dataset")
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))
    p.add_argument("--symbol", default="EURUSD")
    p.add_argument("--from", dest="start", type=_dt, required=True)
    p.add_argument("--to", dest="end", type=_dt, required=True)
    p.add_argument("--seed", type=int, default=7)

    p = sub.add_parser("inspect", help="print a dataset manifest")
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))
    p.add_argument("dataset_id")

    p = sub.add_parser("replay", help="run or resume a replay")
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))
    p.add_argument("--runs", type=Path, default=Path("runs"))
    p.add_argument("--dataset", action="append", required=True)
    p.add_argument("--from", dest="start", type=_dt, required=True)
    p.add_argument("--to", dest="end", type=_dt, required=True)
    p.add_argument("--evaluation-delay-s", type=float, default=0.0)
    p.add_argument("--publication-delay-s", type=float, default=0.0)
    p.add_argument("--code-version", default=None)

    p = sub.add_parser("research", help="run a pre-registered experiment (M2)")
    p.add_argument("prereg", type=Path)
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))
    p.add_argument("--paths", type=Path, default=Path("data/paths"))
    p.add_argument("--runs", type=Path, default=Path("runs"))
    p.add_argument("--research", type=Path, default=Path("research"))
    p.add_argument("--reopen-holdout", action="store_true")
    p.add_argument("--grid", action="store_true", help="informative grid, development only")
    p.add_argument("--code-version", default=None)

    p = sub.add_parser(
        "research-summary", help="side-by-side and pooled summary of experiments (M2)"
    )
    p.add_argument("results", type=Path, nargs="+", help="research/<fingerprint> directories")
    p.add_argument("--out", type=Path, required=True)

    p = sub.add_parser("hypothesis", help="run a pre-registered hypothesis S0-S3/S7 (M4)")
    p.add_argument("prereg", type=Path)
    p.add_argument("--paths", type=Path, default=Path("data/paths"))
    p.add_argument("--research", type=Path, default=Path("research"))
    p.add_argument("--code-version", default=None)

    for name, text in (
        ("h1-check", "H1 cb drift: data check only (no returns)"),
        ("h1-run", "H1 cb drift: run the pre-registration h1-cbdrift-001 (M11)"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("prereg", type=Path)
        p.add_argument("--cache", type=Path, default=Path("data/raw-d1"))
        p.add_argument("--rates", type=Path, default=Path("data/h1-rates/raw"))
        p.add_argument("--calendar", type=Path, default=Path("data/h1-rates/calendar/meetings.csv"))
        p.add_argument("--research", type=Path, default=Path("research"))
        p.add_argument("--code-version", default=None)

    p = sub.add_parser("gotobi", help="E1 gotobi USDJPY: data check, then one confirmation run")
    p.add_argument("prereg", type=Path)
    p.add_argument("--paths", type=Path, default=Path("data/paths"))
    p.add_argument("--research", type=Path, default=Path("research"))
    p.add_argument("--check", action="store_true", help="coverage and spreads only, no returns")

    p = sub.add_parser("g10-download", help="daily BID+ASK candles of the 9 USD pairs (G10)")
    p.add_argument("--cache", type=Path, default=Path("data/raw-d1"))
    p.add_argument("--from-year", type=int, default=2003)
    p.add_argument("--to-year", type=int, required=True)
    p.add_argument("--symbol", action="append", help="default: all 9 pairs")
    p.add_argument("--min-interval", type=float, default=1.0)

    p = sub.add_parser("g10-rates", help="OECD 3-month rates of the 10 currencies from FRED")
    p.add_argument("--out", type=Path, default=Path("data/rates"))

    p = sub.add_parser("g10-check", help="data check of a G10 pre-registration (no results)")
    p.add_argument("prereg", type=Path)
    p.add_argument("--cache", type=Path, default=Path("data/raw-d1"))
    p.add_argument("--rates", type=Path, default=Path("data/rates"))

    p = sub.add_parser("g10-run", help="run a G10 carry/momentum pre-registration (M5)")
    p.add_argument("prereg", type=Path)
    p.add_argument("--cache", type=Path, default=Path("data/raw-d1"))
    p.add_argument("--rates", type=Path, default=Path("data/rates"))
    p.add_argument("--research", type=Path, default=Path("research"))
    p.add_argument("--code-version", default=None)

    p = sub.add_parser("l1-download", help="M10: FRED and Bundesbank series to 2003 (l1 filter)")
    p.add_argument("--out", type=Path, default=Path("data/l1"))
    p.add_argument("--min-interval", type=float, default=2.0)

    p = sub.add_parser("l1-check", help="M10: data check of l1-composito (coverage only)")
    p.add_argument("prereg", type=Path)
    p.add_argument("--data", type=Path, default=Path("data/l1"))

    p = sub.add_parser("l1-run", help="M10: the single run of l1-composito")
    p.add_argument("prereg", type=Path)
    p.add_argument("--data", type=Path, default=Path("data/l1"))
    p.add_argument("--research", type=Path, default=Path("research"))
    p.add_argument("--code-version", default=None)

    p = sub.add_parser("broker-costs", help="print a measured broker cost profile")
    p.add_argument("--profile", default="naga-demo-2026-11-18")
    p.add_argument(
        "--price", action="append", default=[], help="PAIR=MID, overrides the approximate mid"
    )

    p = sub.add_parser("verify", help="verify a run destination")
    p.add_argument("run_dir", type=Path)

    p = sub.add_parser("explain", help="explain the evaluation at an instant")
    p.add_argument("run_dir", type=Path)
    p.add_argument("symbol")
    p.add_argument("at", type=_dt)
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))

    p = sub.add_parser("dashboard", help="start the web app on this machine (read-only)")
    p.add_argument(
        "--host", default="127.0.0.1", help="use your Tailscale IP to reach it from the phone"
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--research", type=Path, default=Path("research"))
    p.add_argument("--prereg", type=Path, action="append", help="default: data/prereg")
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))
    p.add_argument("--raw", type=Path, action="append", help="raw caches; default: data/raw")
    p.add_argument("--frozen", type=Path, action="append", help="folders with frozen documents")
    p.add_argument("--news", type=Path, default=Path("data/news"), help="news archive folder")

    p = sub.add_parser(
        "news-archive", help="save calendar, COT, retail sentiment and central bank statements"
    )
    p.add_argument("--out", type=Path, default=Path("data/news"))
    p.add_argument(
        "--source", action="append", choices=["calendar", "cot", "sentiment", "centralbanks"]
    )

    p = sub.add_parser("notify-test", help="send a Telegram test message")
    p.add_argument("--text", default="FxAgent Next: messaggio di prova. Gli avvisi arrivano qui.")

    from fxnext.execution import cli_demo

    cli_demo.add_parser(sub)
    from fxnext.ai import cli_ai

    cli_ai.add_parser(sub)
    p = sub.choices["dashboard"]
    p.add_argument(
        "--ops", type=Path, default=Path("data/ops/demo.sqlite"), help="demo engine database"
    )
    p.add_argument("--ai", type=Path, default=Path("data/ai"), help="shadow AI archive")

    args = parser.parse_args(argv)
    if args.cmd == "demo":
        return cli_demo.main(args)
    if args.cmd == "ai":
        return cli_ai.main(args)
    if args.cmd == "download":
        bases = [PriceBasis(b) for b in (args.basis or ["BID", "ASK"])]
        summary = download_range(
            RawCache(args.cache),
            args.symbol,
            bases,
            args.start,
            args.end,
            max_attempts=6,
            backoff_seconds=5.0,
            min_interval=args.min_interval,
            progress=_print_progress,
        )
        print(
            json.dumps(
                summary.__dict__
                if hasattr(summary, "__dict__")
                else {k: getattr(summary, k) for k in summary.__slots__}
            )
        )
        return 1 if summary.failed or summary.corrupt else 0
    if args.cmd == "import":
        cache = RawCache(args.cache)
        bid, bid_raw = import_dukascopy(cache, args.symbol, PriceBasis.BID, args.start, args.end)
        ask, ask_raw = import_dukascopy(cache, args.symbol, PriceBasis.ASK, args.start, args.end)
        cross_check_bid_ask(bid, ask)
        t0 = datetime(args.start.year, args.start.month, args.start.day, tzinfo=UTC)
        t1 = datetime(args.end.year, args.end.month, args.end.day, tzinfo=UTC) + timedelta(days=1)
        for basis, res, raw in ((PriceBasis.BID, bid, bid_raw), (PriceBasis.ASK, ask, ask_raw)):
            ref = write_dataset(
                args.datasets,
                res,
                symbol=args.symbol,
                basis=basis,
                provenance=DatasetProvenance.REAL,
                coverage_start=t0,
                coverage_end=t1,
                source={"kind": "dukascopy", "raw_sha256": raw},
            )
            print(basis.value, ref.dataset_id)
        path, _ = import_path_dukascopy(cache, args.symbol, args.start, args.end)
        write_path(args.paths, path)
        print("PATH", path.path_id)
        return 0
    if args.cmd == "synthetic":
        res = synthetic_m15(args.start, args.end, seed=args.seed)
        ref = write_dataset(
            args.datasets,
            res,
            symbol=args.symbol,
            basis=PriceBasis.BID,
            provenance=DatasetProvenance.SYNTHETIC,
            coverage_start=args.start,
            coverage_end=args.end,
            source={"kind": "synthetic", "seed": args.seed},
        )
        print(ref.dataset_id)
        return 0
    if args.cmd == "inspect":
        print(json.dumps(load_dataset(args.datasets, args.dataset_id).manifest, indent=2))
        return 0
    if args.cmd == "replay":
        datasets = [load_dataset(args.datasets, d) for d in args.dataset]
        result = run_replay(
            datasets,
            args.runs,
            args.start,
            args.end,
            evaluation_delay=timedelta(seconds=args.evaluation_delay_s),
            publication_delay=timedelta(seconds=args.publication_delay_s),
            code_version=args.code_version or code_version(),
        )
        assert result is not None
        print(
            json.dumps(
                {
                    "run_fingerprint": result.run_fingerprint,
                    "output_hash": result.output_hash,
                    "evaluations": result.evaluations,
                    "path": str(result.path),
                },
                indent=2,
            )
        )
        return 0
    if args.cmd == "research":
        version = args.code_version or code_version()
        if args.grid:
            rows = run_grid(
                args.prereg,
                datasets_root=args.datasets,
                paths_root=args.paths,
                runs_root=args.runs,
                code_version=version,
            )
            print(json.dumps(rows, indent=2))
            return 0
        exp = run_experiment(
            args.prereg,
            datasets_root=args.datasets,
            paths_root=args.paths,
            runs_root=args.runs,
            research_root=args.research,
            code_version=version,
            reopen_holdout=args.reopen_holdout,
        )
        print(
            json.dumps(
                {
                    "fingerprint": exp.fingerprint,
                    "path": str(exp.path),
                    "outcome": exp.report["criteria"]["outcome"],
                },
                indent=2,
            )
        )
        return 0
    if args.cmd == "research-summary":
        from fxnext.research.aggregate import write_summary

        agg = write_summary(args.results, args.out)
        print(json.dumps({"out": str(args.out), "pooled": agg["pooled"]["exec_x1"]}, indent=2))
        return 0
    if args.cmd in ("h1-check", "h1-run"):
        from fxnext.research.cbdrift.runner import run_cbdrift

        h1 = run_cbdrift(
            args.prereg,
            cache_root=args.cache,
            rates_root=args.rates,
            calendar_path=args.calendar,
            research_root=args.research,
            code_version=args.code_version or code_version(),
            progress=lambda m: print(m, file=sys.stderr, flush=True),
            check_only=args.cmd == "h1-check",
        )
        if args.cmd == "h1-check":
            print(json.dumps(h1.report, indent=2))
            return 0
        print(
            json.dumps(
                {
                    "fingerprint": h1.fingerprint,
                    "path": str(h1.path),
                    "outcome": h1.report["assessment"]["outcome"],
                },
                indent=2,
            )
        )
        return 0
    if args.cmd == "gotobi":
        from fxnext.research.gotobi.run import run as run_gotobi

        rep = run_gotobi(args.prereg, args.paths, args.research, check_only=args.check)
        print(json.dumps({k: rep[k] for k in ("data_check", "assessment") if k in rep}, indent=2))
        return 0
    if args.cmd == "hypothesis":
        from fxnext.research.hyp.runner import run_hypothesis

        hyp = run_hypothesis(
            args.prereg,
            paths_root=args.paths,
            research_root=args.research,
            code_version=args.code_version or code_version(),
            progress=lambda m: print(m, file=sys.stderr, flush=True),
        )
        outcome = hyp.report.get("assessment", {}).get("outcome", "DESCRIPTIVE")
        print(
            json.dumps(
                {"fingerprint": hyp.fingerprint, "path": str(hyp.path), "outcome": outcome},
                indent=2,
            )
        )
        return 0
    if args.cmd == "g10-download":
        from fxnext.data.daily import G10_DIGITS, DailyCache, download_daily

        counts = download_daily(
            DailyCache(args.cache),
            args.symbol or list(G10_DIGITS),
            [PriceBasis.BID, PriceBasis.ASK],
            args.from_year,
            args.to_year,
            min_interval=args.min_interval,
            progress=lambda r: print(
                f"{r['symbol']} {r['basis']} {r['year']}: {r['status']}",
                file=sys.stderr,
                flush=True,
            ),
        )
        print(json.dumps(counts, indent=2))
        return 0 if counts["failed"] == 0 and counts["corrupt"] == 0 else 1
    if args.cmd == "g10-rates":
        from fxnext.data.daily import FRED_SERIES, RatesCache, download_rates

        statuses = download_rates(RatesCache(args.out), list(FRED_SERIES.values()))
        print(json.dumps(statuses, indent=2))
        return 0 if all(s in ("OK", "SKIPPED") for s in statuses.values()) else 1
    if args.cmd in ("g10-check", "g10-run"):
        from fxnext.research.g10.runner import run_g10

        g10 = run_g10(
            args.prereg,
            cache_root=args.cache,
            rates_root=args.rates,
            research_root=getattr(args, "research", Path("research")),
            code_version=getattr(args, "code_version", None) or code_version(),
            progress=lambda m: print(m, file=sys.stderr, flush=True),
            check_only=args.cmd == "g10-check",
        )
        if args.cmd == "g10-check":
            print(json.dumps(g10.report, indent=2))
            return 0
        print(
            json.dumps(
                {
                    "fingerprint": g10.fingerprint,
                    "path": str(g10.path),
                    "outcome": g10.report["assessment"]["outcome"],
                },
                indent=2,
            )
        )
        return 0
    if args.cmd == "l1-download":
        from fxnext.research.composite.sources import download_sources

        statuses = download_sources(
            args.out,
            min_interval=args.min_interval,
            progress=lambda r: print(f"{r['series']}: {r['status']}", file=sys.stderr, flush=True),
        )
        print(json.dumps(statuses, indent=2))
        bad = [k for k, v in statuses.items() if v in ("FAILED", "CORRUPT")]
        return 0 if not bad else 1
    if args.cmd in ("l1-check", "l1-run"):
        from fxnext.research.composite.runner import run_l1

        l1 = run_l1(
            args.prereg,
            data_root=args.data,
            research_root=getattr(args, "research", Path("research")),
            code_version=getattr(args, "code_version", None) or code_version(),
            check_only=args.cmd == "l1-check",
            progress=lambda m: print(m, file=sys.stderr, flush=True),
        )
        if args.cmd == "l1-check":
            print(json.dumps(l1.report, indent=2, default=str))
            return 0
        print(
            json.dumps(
                {
                    "fingerprint": l1.fingerprint,
                    "path": str(l1.path),
                    "outcome": l1.report["assessment"]["outcome"],
                },
                indent=2,
            )
        )
        return 0
    if args.cmd == "broker-costs":
        return _broker_costs(args)
    if args.cmd == "verify":
        manifest = read_manifest(args.run_dir)
        dest = AuditDestination(
            args.run_dir.parent, manifest["run_fingerprint"], manifest["fingerprint_inputs"]
        )
        with dest:
            head = dest.verify_segments()
        ok = manifest["status"] != "COMPLETED" or head == manifest["output_hash"]
        print(json.dumps({"ok": ok, "chain_head": head, "status": manifest["status"]}))
        return 0 if ok else 1
    if args.cmd == "explain":
        print(json.dumps(explain(args.run_dir, args.symbol, args.at, args.datasets), indent=2))
        return 0
    if args.cmd == "dashboard":
        return _dashboard(args)
    if args.cmd == "news-archive":
        from fxnext.news.archive import SOURCES, archive_once

        outcomes = archive_once(args.out, tuple(args.source or SOURCES))
        for name, outcome in outcomes.items():
            print(f"{name}: {outcome}")
        return 1 if any(v.startswith("error") for v in outcomes.values()) else 0
    if args.cmd == "notify-test":
        from fxnext.dashboard.telegram import (
            CHAT_ENV,
            TOKEN_ENV,
            TelegramConfig,
            TelegramError,
            send_message,
        )

        cfg = TelegramConfig.from_env()
        if cfg is None:
            print(f"Imposta {TOKEN_ENV} e {CHAT_ENV} prima di provare.", file=sys.stderr)
            return 1
        try:
            send_message(cfg, args.text)
        except TelegramError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("Messaggio inviato.")
        return 0
    return 2


def _broker_costs(args: argparse.Namespace) -> int:
    from fxnext.research.broker import load_profile

    prices = {}
    for item in args.price:
        pair, _, mid = item.partition("=")
        prices[pair.strip().upper()] = float(mid)
    prof = load_profile(args.profile)
    print(
        f"{prof.name} (sha256 {prof.sha256[:16]}),"
        f" commissione {prof.commission_pips_per_side} pip/lato"
    )
    print("coppia   spread pip  swap long %  swap short %  ricarico/lato %  differenziale %")
    for r in prof.table(prices or None):
        sp = "n/d" if r["spread_pips"] is None else f"{r['spread_pips']:.2f}"
        print(
            f"{r['pair']:8} {sp:>10} {r['swap_long_pct']:>12.2f} {r['swap_short_pct']:>13.2f}"
            f" {r['markup_pct_per_side']:>16.2f} {r['implied_differential_pct']:>16.2f}"
        )
    print("ricarico per valuta (g10-cost-naga-v1):", prof.markup_pct_by_currency(prices or None))
    return 0


def _dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from fxnext.dashboard.app import create_app
    from fxnext.dashboard.readers import Sources

    if args.host in ("0.0.0.0", "::"):
        print(
            "Attenzione: l'app sarà raggiungibile da tutta la rete. "
            "Per il telefono usa l'IP Tailscale del PC.",
            file=sys.stderr,
        )
    sources = Sources(
        research=args.research,
        prereg=tuple(args.prereg or [Path("data/prereg")]),
        datasets=args.datasets,
        raw=tuple(args.raw or [Path("data/raw")]),
        frozen=tuple(args.frozen or []),
        news=args.news,
        ops=args.ops,
        ai=args.ai,
    )
    print(f"FxAgent Next su http://{args.host}:{args.port}  (Ctrl+C per fermare)")
    uvicorn.run(create_app(sources), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
