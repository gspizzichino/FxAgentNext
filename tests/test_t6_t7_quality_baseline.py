"""T6 (bar quality, price basis) and T7 (Donchian baseline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from conftest import make_dataset
from helpers import CAL, CONFIG, H1, STRATEGY, evaluate_at

from fxnext.data.importers import import_generic_csv
from fxnext.data.snapshot import SnapshotBuilder
from fxnext.data.store import load_dataset, write_dataset
from fxnext.data.synthetic import synthetic_m15
from fxnext.domain.contracts import EMPTY_STATE, Candidate, MarketSnapshot, Slot
from fxnext.domain.enums import (
    DatasetProvenance,
    Direction,
    FilterStatus,
    PriceBasis,
    ReasonCode,
    RejectReason,
    SlotStatus,
)
from fxnext.domain.frozen import FrozenMap
from fxnext.domain.series import INT64_MIN, BarSeries
from fxnext.domain.timeutil import from_ns, to_ns
from fxnext.replay.runner import ReplayInputError, run_replay

SIGNAL = datetime(2024, 2, 7, 10, tzinfo=UTC)
M15 = 15 * 60 * 10**9


def _reason(ev):  # type: ignore[no-untyped-def]
    return getattr(ev.outcome, "reason_code", None)


# -- T6 ----------------------------------------------------------------------


def test_t6_1_flat_bar_in_session(tmp_path: Path) -> None:
    flat = to_ns(SIGNAL) - 5 * H1.ns + M15  # inside slot t-5
    ds = make_dataset(tmp_path / "a", flats=[flat])
    assert SlotStatus.FLAT_ZERO_VOLUME in {SlotStatus(int(s)) for s in ds.columns["status"]}
    grid = CAL.slot_grid(H1, SIGNAL - timedelta(days=5), SIGNAL + timedelta(days=5))
    affected = 0
    for open_ns in grid.tolist():
        ev = evaluate_at(ds, from_ns(open_ns))
        window = CAL.window_ending_at(H1, open_ns, 21)
        contains = any(w <= flat < w + H1.ns for w in window.tolist())
        assert (_reason(ev) is ReasonCode.FLAT_BAR_IN_WINDOW) == contains
        affected += contains
    assert affected == 22


def test_t6_2_flat_outside_session_is_filler(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    csv.write_text(
        "open_time,open,high,low,close,volume\n"
        "2024-02-10T12:00:00+00:00,1.1,1.1,1.1,1.1,0\n"  # Saturday
        "2024-02-07T10:00:00+00:00,1.1,1.1,1.1,1.1,0\n"
    )  # Wednesday
    res = import_generic_csv(
        csv, datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 2, 15, tzinfo=UTC)
    )
    assert res.quality["closed_filler"] == 1
    assert [r.status for r in res.rows] == [SlotStatus.FLAT_ZERO_VOLUME]


def test_t6_3_invalid_row(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    csv.write_text(
        "open_time,open,high,low,close,volume\n2024-02-07T10:00:00+00:00,1.10,1.11,1.09,1.12,5\n"
    )
    res = import_generic_csv(
        csv, datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 2, 15, tzinfo=UTC)
    )
    assert res.rejected[0].reason is RejectReason.OHLC_INCONSISTENT
    ds = make_dataset(tmp_path / "b", invalid=[to_ns(SIGNAL) + M15])
    assert _reason(evaluate_at(ds, SIGNAL)) is ReasonCode.INVALID_DATA
    assert _reason(evaluate_at(ds, SIGNAL + timedelta(hours=1))) is ReasonCode.INVALID_DATA


def test_t6_4_duplicates(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    row = "2024-02-07T10:00:00+00:00,1.10,1.12,1.09,1.11,5\n"
    csv.write_text(
        "open_time,open,high,low,close,volume\n"
        + row
        + row
        + "2024-02-07T10:15:00+00:00,1.10,1.12,1.09,1.11,5\n"
        "2024-02-07T10:15:00+00:00,1.10,1.13,1.09,1.11,5\n"
    )
    res = import_generic_csv(
        csv, datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 2, 15, tzinfo=UTC)
    )
    assert res.quality["duplicates_identical"] == 1
    assert [r.open_time_ns for r in res.rows] == [to_ns(SIGNAL)]
    assert [r.reason for r in res.rejected] == [RejectReason.CONFLICTING_DUPLICATE]
    # 4 rows read = 1 accepted + 1 identical duplicate + 2 conflicting merged into 1 rejection
    assert res.quality["rows_read"] == 4 and len(res.rows) == 1 and len(res.rejected) == 1


def test_t6_5_real_and_synthetic_not_mixed(tmp_path: Path) -> None:
    syn = make_dataset(tmp_path / "s")
    res = synthetic_m15(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC))
    ref = write_dataset(
        tmp_path / "r",
        res,
        symbol="GBPUSD",
        basis=PriceBasis.BID,
        provenance=DatasetProvenance.REAL,
        coverage_start=datetime(2024, 1, 1, tzinfo=UTC),
        coverage_end=datetime(2024, 3, 1, tzinfo=UTC),
        source={},
    )
    real = load_dataset(tmp_path / "r", ref.dataset_id)
    with pytest.raises(ReplayInputError):
        run_replay(
            [syn, real], tmp_path / "runs", SIGNAL, SIGNAL + timedelta(hours=2), code_version="t"
        )


def test_t6_6_level_basis(dataset) -> None:
    grid = CAL.slot_grid(H1, datetime(2024, 1, 15, tzinfo=UTC), datetime(2024, 2, 25, tzinfo=UTC))
    seen = 0
    for open_ns in grid.tolist():
        snap = _snap_from(dataset, open_ns)
        ev = STRATEGY.evaluate(snap, CONFIG, EMPTY_STATE)
        if isinstance(ev.outcome, Candidate):
            seen += 1
            assert ev.outcome.level_basis is PriceBasis.BID
            assert ev.outcome.entry_ref == float(snap.series[H1].close[-1])
    assert seen > 0


def _snap_from(dataset, open_ns: int) -> MarketSnapshot:  # type: ignore[no-untyped-def]
    window = CAL.window_ending_at(H1, open_ns, 21)
    return SnapshotBuilder(dataset).build(
        H1, window, CAL.next_slot_open(H1, open_ns), open_ns + H1.ns
    )


# -- T7 ----------------------------------------------------------------------


def _manual(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    statuses: list[int] | None = None,
    signal: datetime = SIGNAL,
) -> MarketSnapshot:
    open_ns = to_ns(signal)
    window = CAL.window_ending_at(H1, open_ns, 21)
    n = window.size
    statuses = statuses or [0] * n
    priced = np.array([s <= 1 for s in statuses])
    opens = [lo if p else np.nan for lo, p in zip(lows, priced, strict=True)]
    cols = {
        "open_time_ns": window,
        "close_time_ns": window + H1.ns,
        "available_at_ns": np.where(priced, window + H1.ns, INT64_MIN),
        "availability": np.where(priced, 1, 255),
        "status": statuses,
        "open": [max(o, lo) if p else np.nan for o, lo, p in zip(opens, lows, priced, strict=True)],
        "high": np.where(priced, highs, np.nan),
        "low": np.where(priced, lows, np.nan),
        "close": np.where(priced, closes, np.nan),
        "volume": np.where(priced, 1.0, np.nan),
        "source_count": [4] * n,
    }
    nxt = CAL.next_slot_open(H1, open_ns)
    from fxnext.domain.contracts import INSTRUMENTS

    return MarketSnapshot(
        symbol="EURUSD",
        instrument=INSTRUMENTS["EURUSD"],
        as_of=from_ns(open_ns + H1.ns),
        signal_slot=Slot(
            timeframe=H1, open_time=from_ns(open_ns), close_time=from_ns(open_ns + H1.ns)
        ),
        next_slot=None
        if nxt is None
        else Slot(timeframe=H1, open_time=from_ns(nxt), close_time=from_ns(nxt + H1.ns)),
        calendar_version=CAL.calendar_version,
        series=FrozenMap({H1: BarSeries("EURUSD", H1, PriceBasis.BID, cols)}),
        quote=None,
        sources=(),
    )


# 22 slots t-21..t, values multiples of 1/1024 so hand arithmetic is exact.
_U = 1 / 1024
HIGHS = [1.0 + k * _U for k in (4, 5, 6, 5, 4, 6, 7, 6, 5, 4, 8, 6, 5, 6, 7, 6, 5, 4, 6, 5, 6, 12)]
LOWS = [1.0 + k * _U for k in (0, 1, 2, 1, 0, 2, 3, 2, 1, 0, 4, 2, 1, 2, 3, 2, 1, 0, 2, 1, 2, 4)]
CLOSES = [1.0 + k * _U for k in (2, 3, 4, 3, 2, 4, 5, 4, 3, 2, 6, 4, 3, 4, 5, 4, 3, 2, 4, 3, 5, 10)]


def test_t7_1_hand_vectors() -> None:
    ev = STRATEGY.evaluate(_manual(HIGHS, LOWS, CLOSES), CONFIG, EMPTY_STATE)
    m = ev.measures
    # channel t-20..t-1 = idx 1..20: max high k=8 (idx 10), min low k=0 (idx 4)
    assert m["channel_upper"] == 1.0 + 8 * _U
    assert m["channel_lower"] == 1.0 + 0 * _U
    assert m["channel_upper_prev"] == 1.0 + 8 * _U
    # TR over t-13..t computed by hand in units of 1/1024:
    tr_units = [4, 4, 6, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 8]  # idx 8..21, see table below
    # e.g. idx 10: H=8 L=4 prevC=2 -> max(4, 6, 2) = 6; idx 21: H=12 L=4 prevC=5 -> 8
    hand = [
        max(h - lo, abs(h - c), abs(lo - c))
        for h, lo, c in zip(HIGHS[8:], LOWS[8:], CLOSES[7:-1], strict=True)
    ]
    assert [round(x / _U) for x in hand] == tr_units
    assert m["atr"] == sum(tr_units) * _U / 14
    assert isinstance(ev.outcome, Candidate) and ev.outcome.direction is Direction.LONG
    d = 1.5 * m["atr"]
    assert ev.outcome.stop == CLOSES[-1] - d
    assert ev.outcome.target == CLOSES[-1] + 2.0 * d
    assert ev.outcome.expires_at == SIGNAL + timedelta(hours=2)


def test_t7_2_equality_and_stale_breakout() -> None:
    eq = CLOSES[:-1] + [1.0 + 8 * _U]
    ev = STRATEGY.evaluate(_manual(HIGHS[:-1] + [1.0 + 9 * _U], LOWS, eq), CONFIG, EMPTY_STATE)
    assert _reason(ev) is ReasonCode.NO_EVENT
    stale_closes = CLOSES[:-2] + [1.0 + 9 * _U, 1.0 + 10 * _U]
    stale_highs = HIGHS[:-2] + [1.0 + 9 * _U, 1.0 + 12 * _U]
    ev2 = STRATEGY.evaluate(_manual(stale_highs, LOWS, stale_closes), CONFIG, EMPTY_STATE)
    assert _reason(ev2) is ReasonCode.NO_EVENT


def test_t7_3_current_bar_excluded() -> None:
    a = STRATEGY.evaluate(_manual(HIGHS, LOWS, CLOSES), CONFIG, EMPTY_STATE)
    b = STRATEGY.evaluate(_manual(HIGHS[:-1] + [1.0 + 50 * _U], LOWS, CLOSES), CONFIG, EMPTY_STATE)
    assert a.measures["channel_upper"] == b.measures["channel_upper"]


def test_t7_4_weekend_not_gap_thursday_gap(tmp_path: Path) -> None:
    monday = datetime(2024, 2, 12, 2, tzinfo=UTC)  # window crosses the weekend
    ds = make_dataset(tmp_path / "a")
    assert _reason(evaluate_at(ds, monday)) not in (
        ReasonCode.DATA_GAP,
        ReasonCode.BAR_NOT_AVAILABLE,
    )
    thursday = to_ns(datetime(2024, 2, 8, 15, tzinfo=UTC))
    gapped = make_dataset(tmp_path / "b", gaps=[thursday + M15])
    grid = CAL.slot_grid(H1, datetime(2024, 2, 8, tzinfo=UTC), datetime(2024, 2, 13, tzinfo=UTC))
    for open_ns in grid.tolist():
        reason = _reason(evaluate_at(gapped, from_ns(open_ns)))
        window = CAL.window_ending_at(H1, open_ns, 21).tolist()
        if open_ns == thursday:
            assert reason is ReasonCode.BAR_NOT_AVAILABLE
        elif thursday in window:
            assert reason is ReasonCode.DATA_GAP
        else:
            assert reason not in (ReasonCode.DATA_GAP, ReasonCode.BAR_NOT_AVAILABLE)


def test_t7_5_warm_up(tmp_path: Path) -> None:
    start = datetime(2024, 2, 5, tzinfo=UTC)  # Monday 00:00, inside the session
    ds = make_dataset(tmp_path / "a", start=start, end=datetime(2024, 3, 1, tzinfo=UTC))
    grid = CAL.slot_grid(H1, start, datetime(2024, 2, 8, tzinfo=UTC))
    reasons = [_reason(evaluate_at(ds, from_ns(x))) for x in grid[:23].tolist()]
    assert all(r is ReasonCode.INSUFFICIENT_HISTORY for r in reasons[:21])
    assert all(r is not ReasonCode.INSUFFICIENT_HISTORY for r in reasons[21:])
    ev = evaluate_at(ds, from_ns(int(grid[21])))
    assert ev.filters[1].status is FilterStatus.PASSED


def test_t7_6_market_closed(dataset, tmp_path: Path) -> None:
    friday_last = datetime(2024, 2, 9, 21, tzinfo=UTC)  # 16:00-17:00 NY (EST)
    assert _reason(evaluate_at(dataset, friday_last)) is ReasonCode.MARKET_CLOSED
    eve = datetime(2024, 12, 24, 21, tzinfo=UTC)  # before the Dec 25 closure
    ds = make_dataset(
        tmp_path / "xmas",
        start=datetime(2024, 12, 1, tzinfo=UTC),
        end=datetime(2025, 1, 10, tzinfo=UTC),
    )
    assert _reason(evaluate_at(ds, eve)) is ReasonCode.MARKET_CLOSED


def test_t7_7_gate_order() -> None:
    st = [0] * 22
    st[5] = int(SlotStatus.INVALID)
    st[8] = int(SlotStatus.UNAVAILABLE)
    ev = STRATEGY.evaluate(_manual(HIGHS, LOWS, CLOSES, st), CONFIG, EMPTY_STATE)
    assert _reason(ev) is ReasonCode.DATA_GAP
    names = [(f.name, f.status) for f in ev.filters]
    assert names[2] == ("window_available", FilterStatus.FAILED)
    assert all(s is FilterStatus.NOT_EVALUATED for _, s in names[3:])


def test_t7_8_finite_memory(tmp_path: Path) -> None:
    a = make_dataset(tmp_path / "a", start=datetime(2024, 1, 1, tzinfo=UTC))
    full = synthetic_m15(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC))
    cut = to_ns(datetime(2024, 1, 29, tzinfo=UTC))
    full.rows = [r for r in full.rows if r.open_time_ns >= cut]
    ref = write_dataset(
        tmp_path / "b",
        full,
        symbol="EURUSD",
        basis=PriceBasis.BID,
        provenance=DatasetProvenance.SYNTHETIC,
        coverage_start=datetime(2024, 1, 29, tzinfo=UTC),
        coverage_end=datetime(2024, 3, 1, tzinfo=UTC),
        source={},
    )
    b = load_dataset(tmp_path / "b", ref.dataset_id)
    ea, eb = evaluate_at(a, SIGNAL), evaluate_at(b, SIGNAL)
    assert ea.outcome == eb.outcome and ea.measures == eb.measures and ea.filters == eb.filters
