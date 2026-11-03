"""T10 metrics/benchmarks, T11 replay/runtime parity, T12 protocol (M2 spec §12)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from conftest import make_dataset

from fxnext.audit.store import iter_rows
from fxnext.data.dukascopy import RawCache
from fxnext.data.importers import cross_check_bid_ask, import_dukascopy
from fxnext.data.path import import_path_dukascopy, write_path
from fxnext.data.store import write_dataset
from fxnext.data.synthetic import synthetic_raw_cache
from fxnext.domain.enums import DatasetProvenance, Direction, PriceBasis
from fxnext.domain.timeutil import to_ns
from fxnext.replay.live import run_live
from fxnext.replay.runner import run_replay
from fxnext.research import benchmarks as bm
from fxnext.research.execution import ExitReason, Fill, Stream
from fxnext.research.experiment import run_experiment
from fxnext.research.metrics import (
    block_bootstrap_ci,
    independent_count,
    max_drawdown,
    percentile_of,
    summarize,
)
from fxnext.research.protocol import (
    HoldoutLog,
    ProtocolError,
    check_development_only,
    load_preregistration,
)

DAY = 86_400 * 1_000_000_000


# ---------------------------------------------------------------- T10 metrics
def test_t10_1_hand_metrics() -> None:
    r = np.array([1.0, -1.0, 2.0, -1.0])
    t = np.arange(4, dtype=np.int64) * DAY
    s = summarize(r, t, t + DAY // 2, months=1.0, bootstrap_samples=200, seed=1)
    assert s.expectancy == 0.25 and s.total_r == 1.0 and s.win_rate == 0.5
    assert s.profit_factor == 1.5 and s.max_drawdown_r == 1.0 and s.trades_per_month == 4.0
    dd, days = max_drawdown(np.array([1.0, -1.0, -1.0, 3.0]), np.arange(4, dtype=np.int64) * DAY)
    assert dd == 2.0 and days == 2.0


def test_t10_2_bootstrap_reproducible_and_contains_mean() -> None:
    rng = np.random.Generator(np.random.PCG64(5))
    r = rng.normal(0.1, 1.0, 400)
    t = np.sort(rng.integers(0, 400 * DAY, 400)).astype(np.int64)
    a = block_bootstrap_ci(r, t, samples=2000, seed=9)
    b = block_bootstrap_ci(r, t, samples=2000, seed=9)
    assert a == b and a is not None and a[0] < r.mean() < a[1]


def test_t10_3_independent_count() -> None:
    entry = np.array([0, 5, 20, 21, 40], dtype=np.int64)
    exit_ = np.array([10, 15, 30, 25, 50], dtype=np.int64)
    assert independent_count(entry, exit_) == 3  # [0,10], [20,30], [40,50]


def _fill(key: str, entry: int, exit_: int, r: float) -> Fill:
    return Fill(key, Stream.EXEC, ExitReason.TARGET, entry, 1.0, exit_, 1.0, r, r)


def test_t10_4_benchmarks() -> None:
    points = [bm.EntryPoint("EURUSD", i * 10, i * 10 + 5, 1.1, 0.001, 0) for i in range(4)]
    pairs = [
        (_fill(f"{i}L", i * 10, i * 10 + 5, 1.0), _fill(f"{i}S", i * 10, i * 10 + 5, -1.0))
        for i in range(4)
    ]
    paired = bm.PairedFills.of(points, pairs)
    assert bm.fixed_direction(paired, Direction.LONG) == 1.0
    assert bm.fixed_direction(paired, Direction.SHORT) == -1.0
    rd = bm.random_direction(paired, 500, seed=3)
    assert np.array_equal(rd, bm.random_direction(paired, 500, seed=3))
    assert rd.min() >= -1.0 and rd.max() <= 1.0 and abs(rd.mean()) < 0.15
    rt = bm.random_time(paired, {0: 2}, 200, seed=4)
    assert rt.size == 200 and set(np.unique(rt)) <= {-1.0, 0.0, 1.0}
    assert percentile_of(1.0, rd) == pytest.approx(100.0 - 50.0 * float(np.mean(rd == 1.0)))


# ------------------------------------------------- end-to-end synthetic data
def _prepare(root: Path, start: date, end: date, *, seed: int = 11) -> tuple[str, str]:
    synthetic_raw_cache(root / "raw", "EURUSD", start, end, seed=seed)
    cache = RawCache(root / "raw")
    bid, _ = import_dukascopy(cache, "EURUSD", PriceBasis.BID, start, end)
    ask, _ = import_dukascopy(cache, "EURUSD", PriceBasis.ASK, start, end)
    cross_check_bid_ask(bid, ask)
    c0 = datetime(start.year, start.month, start.day, tzinfo=UTC)
    c1 = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    ref = write_dataset(
        root / "ds",
        bid,
        symbol="EURUSD",
        basis=PriceBasis.BID,
        provenance=DatasetProvenance.SYNTHETIC,
        coverage_start=c0,
        coverage_end=c1,
        source={"kind": "synthetic"},
    )
    path, _ = import_path_dukascopy(
        cache, "EURUSD", start, end, provenance=DatasetProvenance.SYNTHETIC
    )
    write_path(root / "paths", path)
    return ref.dataset_id, path.path_id


def _prereg(root: Path, ds: str, path: str, split: str, extra: str = "") -> Path:
    p = root / f"prereg-{split}.toml"
    p.write_text(
        f'experiment_id = "t-{split}"\nhypothesis = "test"\nsplit = "{split}"\n'
        f"benchmark_repetitions = 50\nbootstrap_samples = 500\n{extra}\n"
        f'[[data]]\nsymbol = "EURUSD"\nm15_bid_dataset = "{ds}"\npath = "{path}"\n'
    )
    return p


def _run(root: Path, prereg: Path, **kw: object):  # type: ignore[no-untyped-def]
    return run_experiment(
        prereg,
        datasets_root=root / "ds",
        paths_root=root / "paths",
        runs_root=root / "runs",
        research_root=root / "research",
        code_version="test",
        **kw,
    )  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def validation_data(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    root = tmp_path_factory.mktemp("val")
    ds, path = _prepare(root, date(2020, 12, 1), date(2021, 2, 28))
    return root, ds, path


def test_t10_5_costs_monotone_and_report(validation_data: tuple[Path, str, str]) -> None:
    root, ds, path = validation_data
    res = _run(root, _prereg(root, ds, path, "validation"))
    rep = res.report
    x1, x2 = rep["streams"]["exec"]["x1"], rep["streams"]["exec"]["x2"]
    assert x1["trades"] == x2["trades"] > 0 and x2["total_r"] < x1["total_r"]
    import pyarrow.parquet as pq

    rows = pq.read_table(res.path / "outcomes.parquet").to_pylist()
    r1 = {r["key"]: r["r_net"] for r in rows if r["variant"] == "x1" and r["stream"] == "exec"}
    r2 = {r["key"]: r["r_net"] for r in rows if r["variant"] == "x2" and r["stream"] == "exec"}
    assert r1.keys() == r2.keys() and all(r2[k] <= r1[k] for k in r1 if r1[k] is not None)
    assert all(r["provenance"] == "MODELLED" for r in rows)
    assert rep["streams"]["broker"] is None and rep["criteria"]["outcome"] in {
        "REJECTED",
        "INCONCLUSIVE",
        "VALIDATION_PASSED",
    }
    assert any("SYNTHETIC" in w for w in rep["warnings"])
    # same inputs -> same fingerprint and same report (idempotent)
    again = _run(root, _prereg(root, ds, path, "validation"))
    assert again.fingerprint == res.fingerprint and again.report == json.loads(
        (res.path / "report.json").read_text()
    )


# ------------------------------------------------------------- T11 parity
@pytest.mark.parametrize("pub_delay_s", [0, 90])
def test_t11_parity_replay_vs_runtime(tmp_path: Path, pub_delay_s: int) -> None:
    start, end = datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC)
    gap = to_ns(datetime(2024, 1, 16, 10, tzinfo=UTC))
    late_open = to_ns(datetime(2024, 1, 17, 14, 15, tzinfo=UTC))
    ds = make_dataset(
        tmp_path / "ds",
        start=start,
        end=end,
        gaps=[gap],
        flats=[to_ns(datetime(2024, 1, 18, 9, tzinfo=UTC))],
        invalid=[to_ns(datetime(2024, 1, 22, 11, 30, tzinfo=UTC))],
        late={late_open: late_open + 15 * 60 * 10**9 + 2 * 3600 * 10**9},
    )
    r0 = datetime(2024, 1, 8, tzinfo=UTC)
    rep = run_replay(
        [ds],
        tmp_path / "runs",
        r0,
        end,
        code_version="parity",
        publication_delay=timedelta(seconds=pub_delay_s),
    )
    assert rep is not None
    replay_hashes = [row["evaluation_hash"] for row in iter_rows(rep.path)]
    live_hashes = [
        e.evaluation_hash
        for e in run_live(ds, r0, end, publication_delay=timedelta(seconds=pub_delay_s))
    ]
    assert live_hashes == replay_hashes and len(live_hashes) > 400
    # negative control: a runtime that receives bars at other instants is detected
    other = [
        e.evaluation_hash
        for e in run_live(ds, r0, end, publication_delay=timedelta(seconds=pub_delay_s + 3600))
    ]
    assert other != replay_hashes
    # T11.2: the late bar makes both paths abstain at the same instants with the same code
    reasons = [row["reason_code"] for row in iter_rows(rep.path)]
    assert "BAR_NOT_AVAILABLE" in reasons or "DATA_GAP" in reasons


# ------------------------------------------------------------- T12 protocol
def test_t12_1_holdout_requires_preregistration_flag(tmp_path: Path) -> None:
    p = _prereg(tmp_path, "x", "y", "holdout")
    with pytest.raises(ProtocolError, match="holdout = true"):
        load_preregistration(p)


def test_t12_2_holdout_opening_logged_once(tmp_path: Path) -> None:
    root = tmp_path
    ds, path = _prepare(root, date(2023, 12, 1), date(2024, 1, 31), seed=5)
    p = _prereg(root, ds, path, "holdout", "holdout = true\nvalidation_max_drawdown_r = 20.0")
    res = _run(root, p)
    log = HoldoutLog(root / "research").entries()
    assert len(log) == 1 and log[0]["experiment_id"] == "t-holdout"
    assert res.report["criteria"]["outcome"] in {"CANDIDATE_FOR_DEMO", "INCONCLUSIVE", "REJECTED"}
    with pytest.raises(ProtocolError, match="already opened"):
        _run(root, p)
    _run(root, p, reopen_holdout=True)
    log = HoldoutLog(root / "research").entries()
    assert [e["opening"] for e in log] == [1, 2] and log[1]["reopen"] is True


def test_t12_3_prereg_byte_changes_fingerprint(validation_data: tuple[Path, str, str]) -> None:
    root, ds, path = validation_data
    a = _run(root, _prereg(root, ds, path, "validation"))
    p = _prereg(root, ds, path, "validation")
    p.write_text(p.read_text() + " ")
    b = _run(root, p)
    assert a.fingerprint != b.fingerprint
    assert a.report["streams"]["exec"] == b.report["streams"]["exec"]


def test_t12_4_grid_only_on_development() -> None:
    check_development_only(datetime(2015, 1, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))
    with pytest.raises(ProtocolError):
        check_development_only(datetime(2019, 1, 1, tzinfo=UTC), datetime(2021, 6, 1, tzinfo=UTC))


def test_t12_dirty_tree_and_unknown_fields(tmp_path: Path) -> None:
    p = _prereg(tmp_path, "x", "y", "validation", 'surprise = "field"')
    with pytest.raises(ValueError):
        load_preregistration(p)
    from fxnext.replay.runner import ReplayInputError

    ok = _prereg(tmp_path, "x", "y", "development")
    with pytest.raises(ReplayInputError):
        run_experiment(
            ok,
            datasets_root=tmp_path,
            paths_root=tmp_path,
            runs_root=tmp_path,
            research_root=tmp_path,
            code_version="abc-dirty",
        )


def test_t12_4b_grid_runs_on_development_only(tmp_path: Path) -> None:
    from fxnext.research.experiment import run_grid

    ds, path = _prepare(tmp_path, date(2015, 1, 1), date(2015, 2, 28), seed=3)
    rows = run_grid(
        _prereg(tmp_path, ds, path, "development"),
        datasets_root=tmp_path / "ds",
        paths_root=tmp_path / "paths",
        runs_root=tmp_path / "runs",
        code_version="t",
    )
    assert len(rows) == 27 and {r["channel_slots"] for r in rows} == {10, 20, 40}
    with pytest.raises(ProtocolError):
        run_grid(
            _prereg(tmp_path, ds, path, "validation"),
            datasets_root=tmp_path / "ds",
            paths_root=tmp_path / "paths",
            runs_root=tmp_path / "runs",
            code_version="t",
        )
