"""T13 split boundaries and candidate/outcome reconciliation (M2 spec §14, 2026-11-07)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from fxnext.audit.store import iter_rows
from fxnext.data.dukascopy import RawCache
from fxnext.data.importers import cross_check_bid_ask, import_dukascopy
from fxnext.data.path import import_path_dukascopy, load_path, truncate_path, write_path
from fxnext.data.store import write_dataset
from fxnext.data.synthetic import synthetic_raw_cache
from fxnext.domain.enums import DatasetProvenance, Direction, PriceBasis
from fxnext.domain.timeutil import to_ns
from fxnext.research.execution import ExitReason, Fill, Stream, TradeSpec
from fxnext.research.experiment import reconcile, run_experiment
from fxnext.research.protocol import ProtocolError

SPLIT_END = datetime(2021, 1, 1, tzinfo=UTC)  # development -> validation


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


@pytest.fixture(scope="module")
def across(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str | Path]:
    """Synthetic data from 2020-11-01 to 2021-01-31: it crosses the development end."""

    root = tmp_path_factory.mktemp("across")
    start, end = date(2020, 11, 1), date(2021, 1, 31)
    synthetic_raw_cache(root / "raw", "EURUSD", start, end, seed=13)
    cache = RawCache(root / "raw")
    bid, _ = import_dukascopy(cache, "EURUSD", PriceBasis.BID, start, end)
    ask, _ = import_dukascopy(cache, "EURUSD", PriceBasis.ASK, start, end)
    cross_check_bid_ask(bid, ask)
    ref = write_dataset(
        root / "ds",
        bid,
        symbol="EURUSD",
        basis=PriceBasis.BID,
        provenance=DatasetProvenance.SYNTHETIC,
        coverage_start=_utc(start),
        coverage_end=_utc(end) + timedelta(days=1),
        source={"kind": "synthetic"},
    )
    long_path, _ = import_path_dukascopy(
        cache, "EURUSD", start, end, provenance=DatasetProvenance.SYNTHETIC
    )
    short_path, _ = import_path_dukascopy(
        cache, "EURUSD", start, date(2020, 12, 31), provenance=DatasetProvenance.SYNTHETIC
    )
    write_path(root / "paths", long_path)
    write_path(root / "paths", short_path)
    return {
        "root": root,
        "ds": ref.dataset_id,
        "long": long_path.path_id,
        "short": short_path.path_id,
    }


def _prereg(root: Path, name: str, ds: str, path: str, split: str) -> Path:
    p = root / f"{name}.toml"
    p.write_text(
        f'experiment_id = "{name}"\nhypothesis = "boundary test"\nsplit = "{split}"\n'
        "benchmark_repetitions = 20\nbootstrap_samples = 200\n"
        f'[[data]]\nsymbol = "EURUSD"\nm15_bid_dataset = "{ds}"\npath = "{path}"\n'
    )
    return p


def _run(root: Path, prereg: Path):  # type: ignore[no-untyped-def]
    return run_experiment(
        prereg,
        datasets_root=root / "ds",
        paths_root=root / "paths",
        runs_root=root / "runs",
        research_root=root / "research",
        code_version="t13",
    )


def test_t13_1_truncate_path_is_the_path_imported_to_the_split_end(
    across: dict[str, str | Path],
) -> None:
    root = Path(across["root"])
    long_path = load_path(root / "paths", str(across["long"]))
    short_path = load_path(root / "paths", str(across["short"]))
    assert long_path.coverage_end_ns > to_ns(SPLIT_END) == short_path.coverage_end_ns
    cut = truncate_path(long_path, to_ns(SPLIT_END))
    assert int(cut.columns["t_ns"].max()) < to_ns(SPLIT_END)
    assert cut.coverage_end_ns == to_ns(SPLIT_END)
    assert cut.columns["t_ns"].tolist() == short_path.columns["t_ns"].tolist()
    assert truncate_path(short_path, to_ns(SPLIT_END)) is short_path


def test_t13_2_no_trade_reads_prices_of_the_next_split(across: dict[str, str | Path]) -> None:
    """A development experiment gives the same outcomes whether the path stops at the
    split end or runs into validation: exits never use prices after the split."""

    root, ds = Path(across["root"]), str(across["ds"])
    a = _run(root, _prereg(root, "short", ds, str(across["short"]), "development"))
    b = _run(root, _prereg(root, "long", ds, str(across["long"]), "development"))
    assert a.report["interval"]["end"] == b.report["interval"]["end"] == SPLIT_END.isoformat()
    assert a.report["streams"] == b.report["streams"]
    assert a.report["reconciliation"] == b.report["reconciliation"]
    assert b.report["inputs"]["source_paths"]["EURUSD"] == across["long"]
    for rep in (a.report, b.report):
        rec = rep["reconciliation"]["EURUSD"]
        assert rec["balanced"] and rec["candidates"] > 0
        assert rec["entries_outside_split"] == rec["exits_outside_split"] == 0
        assert (
            rec["exec_executed"] + rec["exec_counterfactual"] + rec["exec_not_entered"]
            == rec["candidates"]
            == rec["signal_fills"]
        )


def test_t13_3_warm_up_uses_previous_split_data(across: dict[str, str | Path]) -> None:
    """Validation starts 2021-01-01 but the dataset holds Nov-Dec 2020: indicators
    have their history from day one (M2 spec §8 warm-up) and no candidate predates
    the split."""

    root, ds = Path(across["root"]), str(across["ds"])
    res = _run(root, _prereg(root, "val", ds, str(across["long"]), "validation"))
    run_dir = next((root / "runs").iterdir())
    reasons = []
    for d in (root / "runs").iterdir():
        rows = list(iter_rows(d))
        if rows and min(int(r["as_of_ns"]) for r in rows) >= to_ns(SPLIT_END):
            reasons = [r["reason_code"] for r in rows]
            run_dir = d
    assert reasons, run_dir
    assert "INSUFFICIENT_HISTORY" not in reasons
    rec = res.report["reconciliation"]["EURUSD"]
    assert rec["candidates_available_before_start"] == 0 and rec["balanced"]


def _fill(key: str, reason: ExitReason, entry: int | None, exit_: int | None) -> Fill:
    r = None if reason in (ExitReason.END_OF_DATA, ExitReason.NOT_FILLED_NO_QUOTE) else 1.0
    return Fill(key, Stream.EXEC, reason, entry, 1.0 if entry else None, exit_, 1.0, r, r)


def test_t13_4_reconcile_refuses_reports_that_do_not_add_up() -> None:
    cands = [
        TradeSpec(k, "EURUSD", Direction.LONG, 1.1, 1.09, 1.12, t, t + 100)
        for k, t in (("a", 10), ("b", 20), ("c", 30))
    ]
    sig = [_fill(c.key, ExitReason.TARGET, c.available_at_ns, c.available_at_ns + 5) for c in cands]
    ex = [
        _fill("a", ExitReason.TARGET, 10, 25),
        _fill("b", ExitReason.STOP, 21, 26),
        _fill("c", ExitReason.NOT_FILLED_NO_QUOTE, None, None),
    ]
    ok = reconcile(cands, sig, ex, [ex[0]], [ex[1]], 0, 1000)
    assert ok["balanced"] and ok["exec_not_entered"] == 1
    with pytest.raises(ProtocolError, match="not entered"):
        reconcile(cands, sig, ex, [ex[0]], [], 0, 1000)  # a candidate lost
    with pytest.raises(ProtocolError, match="both executed and counterfactual"):
        reconcile(cands, sig, ex, [ex[0]], [ex[0]], 0, 1000)
    with pytest.raises(ProtocolError, match="exits_outside_split"):
        reconcile(cands, sig, ex, [ex[0]], [ex[1]], 0, 26)
    with pytest.raises(ProtocolError, match="one to one"):
        reconcile(cands, sig[:2], ex, [ex[0]], [ex[1]], 0, 1000)


# ------------------------------------------------------ cross-symbol summary
def test_t13_5_overlap_clusters() -> None:
    import numpy as np

    from fxnext.research.aggregate import overlap_clusters

    entry = np.array([0, 5, 20, 40, 45], dtype=np.int64)
    exit_ = np.array([10, 12, 30, 50, 46], dtype=np.int64)
    cluster, peak, mean_open = overlap_clusters(entry, exit_)
    assert cluster.tolist() == [0, 0, 1, 2, 2] and peak == 2
    # open time: [0,5) 1, [5,10) 2, [10,12) 1, [20,30) 1, [40,45) 1, [45,46) 2, [46,50) 1
    assert mean_open == pytest.approx((5 + 10 + 2 + 10 + 5 + 2 + 4) / 32)


def _symbol_experiment(root: Path, symbol: str, seed: int, split: str = "development") -> Path:
    start, end = date(2015, 1, 1), date(2015, 3, 31)
    raw = root / f"raw-{symbol}"
    synthetic_raw_cache(raw, symbol, start, end, seed=seed)
    cache = RawCache(raw)
    bid, _ = import_dukascopy(cache, symbol, PriceBasis.BID, start, end)
    ref = write_dataset(
        root / "ds",
        bid,
        symbol=symbol,
        basis=PriceBasis.BID,
        provenance=DatasetProvenance.SYNTHETIC,
        coverage_start=_utc(start),
        coverage_end=_utc(end) + timedelta(days=1),
        source={"kind": "synthetic"},
    )
    path, _ = import_path_dukascopy(
        cache, symbol, start, end, provenance=DatasetProvenance.SYNTHETIC
    )
    write_path(root / "paths", path)
    p = root / f"{symbol}-{split}.toml"
    p.write_text(
        f'experiment_id = "{symbol.lower()}-{split}"\nhypothesis = "t"\nsplit = "{split}"\n'
        "benchmark_repetitions = 20\nbootstrap_samples = 200\n"
        f'[[data]]\nsymbol = "{symbol}"\nm15_bid_dataset = "{ref.dataset_id}"\n'
        f'path = "{path.path_id}"\n'
    )
    return _run(root, p).path  # type: ignore[no-any-return]


def test_t13_6_summary_keeps_experiments_and_flags_overlaps(tmp_path: Path) -> None:
    import json

    from fxnext.research.aggregate import aggregate, render_summary, write_summary

    a = _symbol_experiment(tmp_path, "EURUSD", 21)
    b = _symbol_experiment(tmp_path, "GBPUSD", 22)
    before = {p: p.read_bytes() for d in (a, b) for p in d.iterdir()}
    s = write_summary([a, b], tmp_path / "summary")
    assert {p: p.read_bytes() for d in (a, b) for p in d.iterdir()} == before  # read only
    per = s["per_symbol"]
    assert set(per) == {"EURUSD", "GBPUSD"}
    pooled, ov = s["pooled"]["exec_x1"], s["pooled"]["overlap"]
    assert (
        pooled["trades"] == per["EURUSD"]["exec_x1"]["trades"] + per["GBPUSD"]["exec_x1"]["trades"]
    )
    assert pooled["total_r"] == pytest.approx(
        per["EURUSD"]["exec_x1"]["total_r"] + per["GBPUSD"]["exec_x1"]["total_r"]
    )
    assert ov["clusters"] <= ov["trades"] and ov["clusters_across_symbols"] <= ov["clusters"]
    assert ov["max_open_at_once"] <= 2  # one position per symbol
    assert "non è un portafoglio" in render_summary(s)
    assert json.loads((tmp_path / "summary" / "summary.json").read_text())["split"] == "development"
    with pytest.raises(ProtocolError, match="more than one experiment"):
        aggregate([a, a])
