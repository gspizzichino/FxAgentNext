"""Command line: download, import, inspect, replay run/verify, explain."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fxnext.audit.explain import explain
from fxnext.audit.store import AuditDestination, read_manifest
from fxnext.data.dukascopy import RawCache, download_range
from fxnext.data.importers import cross_check_bid_ask, import_dukascopy
from fxnext.data.store import load_dataset, write_dataset
from fxnext.data.synthetic import synthetic_m15
from fxnext.domain.enums import DatasetProvenance, PriceBasis
from fxnext.replay.runner import run_replay


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fxnext")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("download", help="download Dukascopy M1 candles into the raw cache")
    p.add_argument("--cache", type=Path, default=Path("data/raw/dukascopy"))
    p.add_argument("--symbol", action="append", required=True)
    p.add_argument("--basis", action="append", choices=["BID", "ASK"])
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True)

    p = sub.add_parser("import", help="build M15 datasets from the raw cache")
    p.add_argument("--cache", type=Path, default=Path("data/raw/dukascopy"))
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))
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

    p = sub.add_parser("verify", help="verify a run destination")
    p.add_argument("run_dir", type=Path)

    p = sub.add_parser("explain", help="explain the evaluation at an instant")
    p.add_argument("run_dir", type=Path)
    p.add_argument("symbol")
    p.add_argument("at", type=_dt)
    p.add_argument("--datasets", type=Path, default=Path("data/datasets"))

    args = parser.parse_args(argv)
    if args.cmd == "download":
        bases = [PriceBasis(b) for b in (args.basis or ["BID", "ASK"])]
        summary = download_range(RawCache(args.cache), args.symbol, bases, args.start, args.end)
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
