"""T1 (availability) and T2 (scheduling)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from conftest import END, START, make_dataset
from helpers import CAL, H1, evaluate_at, snapshot_at
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fxnext.audit.explain import explain
from fxnext.data.importers import import_generic_csv
from fxnext.data.rows import ImportResult
from fxnext.data.snapshot import SnapshotBuilder
from fxnext.data.store import load_dataset, write_dataset
from fxnext.data.synthetic import synthetic_m15
from fxnext.domain.contracts import MarketSnapshot, SnapshotInvariantError
from fxnext.domain.enums import (
    Availability,
    DatasetProvenance,
    Dispatch,
    PriceBasis,
    ReasonCode,
    RejectReason,
    SlotStatus,
)
from fxnext.domain.series import BarSeries, SeriesInvariantError
from fxnext.domain.timeutil import to_ns
from fxnext.replay.runner import classify_dispatch, run_replay

SIGNAL = datetime(2024, 2, 7, 10, tzinfo=UTC)  # Wednesday, 10:00-11:00 UTC slot
M15 = 15 * 60 * 10**9


def m15_of(slot: datetime) -> list[int]:
    return [to_ns(slot) + k * M15 for k in range(4)]


def test_t1_1_available_before_close_rejected() -> None:
    cols = {
        "open_time_ns": [0],
        "close_time_ns": [H1.ns],
        "available_at_ns": [H1.ns - 1],
        "availability": [1],
        "status": [0],
        "open": [1.0],
        "high": [1.0],
        "low": [1.0],
        "close": [1.0],
        "volume": [0.0],
        "source_count": [4],
    }
    with pytest.raises(SeriesInvariantError):
        BarSeries("EURUSD", H1, PriceBasis.BID, cols)


def test_t1_1_every_priced_row_has_availability(dataset) -> None:
    series = snapshot_at(dataset, SIGNAL).series[H1]
    priced = series.status <= SlotStatus.FLAT_ZERO_VOLUME
    assert priced.all()
    assert (series.available_at_ns[priced] >= series.close_time_ns[priced]).all()
    assert set(series.availability[priced].tolist()) <= {0, 1}


def test_t1_2_derived_availability(tmp_path: Path) -> None:
    base = to_ns(SIGNAL)
    secs = [5, 2, 9, 3]  # observed seconds after each M15 close
    late = {m: m + M15 + s * 10**9 for m, s in zip(m15_of(SIGNAL), secs, strict=True)}
    ds = make_dataset(tmp_path / "a", late=late)
    snap = snapshot_at(ds, SIGNAL, as_of_delay=timedelta(minutes=1))
    s = snap.series[H1]
    assert s.status[-1] == SlotStatus.VALID
    assert int(s.available_at_ns[-1]) == base + H1.ns + 3 * 10**9
    assert s.availability_of(len(s) - 1) is Availability.OBSERVED
    partial = dict(list(late.items())[:3])
    ds2 = make_dataset(tmp_path / "b", late=partial)
    s2 = snapshot_at(ds2, SIGNAL, as_of_delay=timedelta(minutes=1)).series[H1]
    assert s2.availability_of(len(s2) - 1) is Availability.MODELLED


def test_t1_3_unavailable_then_valid(tmp_path: Path) -> None:
    m = m15_of(SIGNAL)[1]
    ds = make_dataset(tmp_path / "a", late={m: to_ns(SIGNAL) + H1.ns + 30 * 10**9})
    early = snapshot_at(ds, SIGNAL, as_of_delay=timedelta(seconds=10)).series[H1]
    later = snapshot_at(ds, SIGNAL, as_of_delay=timedelta(seconds=30)).series[H1]
    assert early.status[-1] == SlotStatus.UNAVAILABLE
    assert later.status[-1] == SlotStatus.VALID


def _rebuild(good: MarketSnapshot, series: BarSeries, as_of: datetime) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=good.symbol,
        instrument=good.instrument,
        as_of=as_of,
        signal_slot=good.signal_slot,
        next_slot=good.next_slot,
        calendar_version=good.calendar_version,
        series={H1: series},
        quote=None,
        sources=good.sources,
    )


def test_t1_4_snapshot_rejects_future_bar(tmp_path: Path) -> None:
    m = m15_of(SIGNAL)[2]
    ds = make_dataset(tmp_path / "a", late={m: to_ns(SIGNAL) + H1.ns + 60 * 10**9})
    good = snapshot_at(ds, SIGNAL)
    window = CAL.window_ending_at(H1, to_ns(SIGNAL), 21)
    later = SnapshotBuilder(ds).series(H1, window, to_ns(SIGNAL) + H1.ns + 120 * 10**9)
    # bar available 60 s after as_of
    with pytest.raises(ValueError, match="available_at > as_of"):
        _rebuild(good, later, SIGNAL + H1.duration)
    # slot closing after as_of
    with pytest.raises(ValueError, match="slot closing after as_of"):
        _rebuild(good, good.series[H1], SIGNAL + timedelta(minutes=59))
    assert issubclass(SnapshotInvariantError, ValueError)


def _splice(cutoff_ns: int, seed_future: int) -> ImportResult:
    past = synthetic_m15(START, END, seed=7)
    future = synthetic_m15(START, END, seed=seed_future)
    out = ImportResult()
    out.rows = [r for r in past.rows if r.available_at_ns <= cutoff_ns] + [
        r for r in future.rows if r.available_at_ns > cutoff_ns
    ]
    return out


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    hours=st.integers(min_value=0, max_value=200), seed=st.integers(min_value=8, max_value=10**6)
)
def test_t1_5_future_perturbation(tmp_path: Path, hours: int, seed: int) -> None:
    grid = CAL.slot_grid(H1, datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 2, 20, tzinfo=UTC))
    signal = int(grid[hours % grid.size])
    as_of = signal + H1.ns
    root = tmp_path / f"p{hours}-{seed}"
    refs = []
    for res in (synthetic_m15(START, END, seed=7), _splice(as_of, seed)):
        refs.append(
            write_dataset(
                root,
                res,
                symbol="EURUSD",
                basis=PriceBasis.BID,
                provenance=DatasetProvenance.SYNTHETIC,
                coverage_start=START,
                coverage_end=END,
                source={},
            )
        )
    a, b = (load_dataset(root, r.dataset_id) for r in refs)
    from fxnext.domain.timeutil import from_ns

    ea, eb = evaluate_at(a, from_ns(signal)), evaluate_at(b, from_ns(signal))
    assert snapshot_at(a, from_ns(signal)).series[H1] == snapshot_at(b, from_ns(signal)).series[H1]
    assert ea.outcome == eb.outcome and ea.filters == eb.filters and ea.measures == eb.measures


def test_t1_6_csv_available_before_close_rejected(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    csv.write_text(
        "open_time,open,high,low,close,volume,available_at\n"
        "2024-02-07T10:00:00+00:00,1.1,1.2,1.0,1.1,5,2024-02-07T10:10:00+00:00\n"
    )
    res = import_generic_csv(csv, START, END)
    assert not res.rows and res.rejected[0].reason is RejectReason.OFF_GRID


def test_t2_1_late_vs_missing(tmp_path: Path) -> None:
    m = m15_of(SIGNAL)[3]
    late = make_dataset(tmp_path / "late", late={m: to_ns(SIGNAL) + H1.ns + 10**9})
    missing = make_dataset(tmp_path / "missing", gaps=[m])
    el, em = evaluate_at(late, SIGNAL), evaluate_at(missing, SIGNAL)
    assert el.outcome.reason_code is ReasonCode.BAR_NOT_AVAILABLE  # type: ignore[union-attr]
    assert el.outcome == em.outcome and el.filters == em.filters
    assert snapshot_at(late, SIGNAL).series[H1] == snapshot_at(missing, SIGNAL).series[H1]
    runs = {}
    for name, ds in (("late", late), ("missing", missing)):
        r = run_replay(
            [ds],
            tmp_path / f"runs-{name}",
            SIGNAL - timedelta(hours=1),
            SIGNAL + timedelta(hours=2),
            code_version="t",
        )
        assert r is not None
        runs[name] = explain(
            r.path, "EURUSD", SIGNAL + timedelta(hours=1, minutes=5), tmp_path / name
        )
    assert runs["late"]["ex_post"]["m15"][0]["cause"] == "LATE_ARRIVAL"
    assert runs["missing"]["ex_post"]["m15"][0]["cause"] == "MISSING"


def _count(start: datetime, end: datetime, tmp_path: Path, dataset) -> tuple[int, str]:
    r = run_replay([dataset], tmp_path, start, end, code_version="t")
    assert r is not None
    from fxnext.audit.store import iter_rows

    rows = list(iter_rows(r.path))
    from fxnext.domain.timeutil import from_ns

    return len(rows), from_ns(rows[0]["as_of_ns"]).isoformat()


def test_t2_2_one_request_per_expected_close(tmp_path: Path) -> None:
    ds = make_dataset(
        tmp_path / "ds",
        start=datetime(2024, 2, 1, tzinfo=UTC),
        end=datetime(2024, 4, 15, tzinfo=UTC),
    )
    us = _count(
        datetime(2024, 3, 10, tzinfo=UTC), datetime(2024, 3, 16, tzinfo=UTC), tmp_path / "us", ds
    )
    assert us == (120, "2024-03-10T22:00:00+00:00")  # first DST week: open 21:00 UTC
    eu = _count(
        datetime(2024, 3, 31, tzinfo=UTC), datetime(2024, 4, 6, tzinfo=UTC), tmp_path / "eu", ds
    )
    assert eu == (120, "2024-03-31T22:00:00+00:00")  # EU-only change: no effect
    winter = _count(
        datetime(2024, 2, 4, tzinfo=UTC), datetime(2024, 2, 10, tzinfo=UTC), tmp_path / "w", ds
    )
    assert winter == (120, "2024-02-04T23:00:00+00:00")


def test_t2_3_evaluation_delay(tmp_path: Path, dataset) -> None:
    a = run_replay([dataset], tmp_path / "a", SIGNAL, SIGNAL + timedelta(hours=3), code_version="t")
    b = run_replay(
        [dataset],
        tmp_path / "b",
        SIGNAL,
        SIGNAL + timedelta(hours=3),
        evaluation_delay=timedelta(seconds=20),
        code_version="t",
    )
    assert a and b and a.run_fingerprint != b.run_fingerprint
    from fxnext.audit.store import iter_rows

    ra, rb = list(iter_rows(a.path)), list(iter_rows(b.path))
    assert [r["as_of_ns"] + 20 * 10**9 for r in ra] == [r["as_of_ns"] for r in rb]
    assert {r["request_id"] for r in ra}.isdisjoint({r["request_id"] for r in rb})


def test_t2_4_dispatch_classification(dataset) -> None:
    as_of = SIGNAL + timedelta(hours=1)
    lim = timedelta(seconds=30)
    assert classify_dispatch(as_of, None, lim) is Dispatch.REPLAY
    assert classify_dispatch(as_of, as_of + lim, lim) is Dispatch.DISPATCHED
    assert (
        classify_dispatch(as_of, as_of + lim + timedelta(microseconds=1), lim)
        is Dispatch.NOT_DISPATCHED_LATE
    )
    e1 = evaluate_at(dataset, SIGNAL)
    e2 = evaluate_at(dataset, SIGNAL)
    assert e1.evaluation_hash == e2.evaluation_hash  # dispatch never enters the evaluation
    assert np.isfinite(e1.measures.get("atr", 1.0))
