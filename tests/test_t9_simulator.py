"""T9: execution simulator on hand-built M1 BID/ASK paths (M2 spec §3-§5)."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from fxnext.data.dukascopy import RawCache
from fxnext.data.path import (
    MinuteStatus,
    PathIntegrityError,
    PricePath,
    build_path,
    import_path_dukascopy,
    load_path,
    write_path,
)
from fxnext.data.synthetic import synthetic_raw_cache
from fxnext.domain.enums import DatasetProvenance, Direction
from fxnext.domain.hashing import content_hash
from fxnext.domain.timeutil import to_ns
from fxnext.research.execution import (
    CostModel,
    ExecParams,
    ExitReason,
    Fill,
    Simulator,
    Stream,
    TradeSpec,
    apply_position_policy,
)

COV0 = datetime(2024, 1, 1, tzinfo=UTC)
COV1 = datetime(2024, 2, 1, tzinfo=UTC)
T0 = datetime(2024, 1, 9, 10, tzinfo=UTC)  # Tuesday, in session
MIN = 60_000_000_000
PIP = 1e-4
EXEC = ExecParams()
COSTS = CostModel()


def minute(
    t: datetime,
    bid: tuple[float, float, float, float],
    spread: float = PIP,
    ask: tuple[float, float, float, float] | None = None,
    status: MinuteStatus = MinuteStatus.VALID,
) -> dict[str, object]:
    a = ask or tuple(x + spread for x in bid)
    return {
        "t_ns": to_ns(t),
        "status": int(status),
        "bid_open": bid[0],
        "bid_high": bid[1],
        "bid_low": bid[2],
        "bid_close": bid[3],
        "ask_open": a[0],
        "ask_high": a[1],
        "ask_low": a[2],
        "ask_close": a[3],
    }


def make_path(rows: list[dict[str, object]]) -> PricePath:
    cols = {k: [r[k] for r in rows] for k in rows[0]}
    return build_path("EURUSD", DatasetProvenance.SYNTHETIC, COV0, COV1, cols)  # type: ignore[arg-type]


def flat_rows(start: datetime, n: int, price: float = 1.1000) -> list[dict[str, object]]:
    return [minute(start + timedelta(minutes=k), (price, price, price, price)) for k in range(n)]


def spec(
    direction: Direction = Direction.LONG,
    entry: float = 1.1000,
    d: float = 0.0015,
    at: datetime = T0,
    key: str = "c1",
) -> TradeSpec:
    sign = 1.0 if direction is Direction.LONG else -1.0
    return TradeSpec(
        key,
        "EURUSD",
        direction,
        entry,
        entry - sign * d,
        entry + sign * 2 * d,
        to_ns(at),
        to_ns(at + timedelta(hours=1)),
    )


def test_t9_1_long_stop_hand_computed() -> None:
    rows = [
        minute(T0, (1.1000, 1.1002, 1.0998, 1.1001)),
        minute(T0 + timedelta(minutes=1), (1.1001, 1.1001, 1.0984, 1.0990)),
    ] + flat_rows(T0 + timedelta(minutes=2), 5, 1.0990)
    f = Simulator(make_path(rows)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    assert f.reason is ExitReason.STOP and f.entry_ns == to_ns(T0)
    assert f.entry_price == pytest.approx(1.1001 + 0.1 * PIP, abs=1e-12)
    assert f.exit_price == pytest.approx(1.0985 - 0.2 * PIP, abs=1e-12)
    r_gross = (1.0985 - 0.2 * PIP - (1.1001 + 0.1 * PIP)) / 0.0015
    assert f.r_gross == pytest.approx(r_gross, abs=1e-9)
    assert f.r_net == pytest.approx(r_gross - 0.7 / 15, abs=1e-9)
    assert f.spread_pips == pytest.approx(1.0) and f.slippage_pips == pytest.approx(0.3)
    sig = Simulator(make_path(rows)).simulate(spec(), Stream.SIGNAL, EXEC, COSTS)
    assert sig.entry_price == 1.1000 and sig.r_net == pytest.approx(-1.0)


def test_t9_2_short_exits_on_ask_spread_widening() -> None:
    rows = [
        minute(T0, (1.1000, 1.1001, 1.0999, 1.1000)),
        minute(
            T0 + timedelta(minutes=1),
            (1.1000, 1.1010, 1.0999, 1.1005),
            ask=(1.1001, 1.1016, 1.1000, 1.1006),
        ),
    ] + flat_rows(T0 + timedelta(minutes=2), 5, 1.1005)
    sim = Simulator(make_path(rows))
    s = spec(Direction.SHORT)
    f = sim.simulate(s, Stream.EXEC, EXEC, COSTS)
    assert f.reason is ExitReason.STOP and f.exit_ns == to_ns(T0 + timedelta(minutes=1))
    assert f.entry_price == pytest.approx(1.1000 - 0.1 * PIP, abs=1e-12)
    sig = sim.simulate(s, Stream.SIGNAL, EXEC, COSTS)  # BID never reached 1.1015
    assert sig.exit_ns != f.exit_ns


def test_t9_3_ambiguous_minute() -> None:
    rows = [
        minute(T0, (1.1000, 1.1001, 1.0999, 1.1000)),
        minute(T0 + timedelta(minutes=1), (1.1000, 1.1031, 1.0984, 1.1000)),
    ] + flat_rows(T0 + timedelta(minutes=2), 3)
    sim = Simulator(make_path(rows))
    f = sim.simulate(spec(), Stream.EXEC, EXEC, COSTS)
    assert f.reason is ExitReason.STOP and f.ambiguous
    g = sim.simulate(
        spec(), Stream.EXEC, EXEC.model_copy(update={"ambiguity": "TARGET_FIRST"}), COSTS
    )
    assert g.reason is ExitReason.TARGET and g.ambiguous
    assert g.exit_price == pytest.approx(1.1030)


def test_t9_4_weekend_gap() -> None:
    fri = datetime(2024, 1, 12, 21, 58, tzinfo=UTC)  # session closes 22:00 UTC (EST)
    sun = datetime(2024, 1, 14, 22, 0, tzinfo=UTC)
    base = [minute(fri, (1.1000,) * 4), minute(fri + timedelta(minutes=1), (1.1000,) * 4)]
    s = replace(spec(at=fri), expires_at_ns=to_ns(fri + timedelta(minutes=2)))
    down = make_path(
        base
        + [minute(sun, (1.0970, 1.0975, 1.0960, 1.0972))]
        + flat_rows(sun + timedelta(minutes=1), 3, 1.0972)
    )
    f = Simulator(down).simulate(s, Stream.EXEC, EXEC, COSTS)
    assert f.reason is ExitReason.STOP_GAP and f.exit_price == 1.0970 and f.weekend_crossings == 1
    up = make_path(
        base
        + [minute(sun, (1.1040, 1.1045, 1.1035, 1.1040))]
        + flat_rows(sun + timedelta(minutes=1), 3, 1.104)
    )
    g = Simulator(up).simulate(s, Stream.EXEC, EXEC, COSTS)
    assert g.reason is ExitReason.TARGET and g.exit_price == pytest.approx(1.1030)
    assert not f.path_gap and not g.path_gap


def test_t9_5_time_exit_after_120_session_hours() -> None:
    # 120 in-session H1 slots from Tuesday 10:00 UTC end next Tuesday 10:00 UTC.
    rows = []
    t = T0
    end = T0 + timedelta(days=7, hours=2)
    from fxnext.domain.calendar import FxCalendar

    cal = FxCalendar()
    while t < end:
        if cal.is_in_session(t):
            rows.append(minute(t, (1.1000,) * 4))
        t += timedelta(minutes=1)
    f = Simulator(make_path(rows)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    assert f.reason is ExitReason.TIME
    assert f.exit_ns == to_ns(T0 + timedelta(days=7) - timedelta(minutes=1))
    assert f.weekend_crossings == 1 and f.holding_slots == 120
    assert f.exit_price == pytest.approx(1.1000 - 0.2 * PIP)


def test_t9_6_no_quote_at_entry() -> None:
    rows = [minute(T0 + timedelta(minutes=1), (1.1000,) * 4)] + flat_rows(
        T0 + timedelta(minutes=2), 3
    )
    f = Simulator(make_path(rows)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    assert f.entry_ns == to_ns(T0 + timedelta(minutes=1))
    flat = [
        minute(T0 + timedelta(minutes=k), (1.1,) * 4, status=MinuteStatus.FLAT) for k in range(60)
    ]
    late = [minute(T0 + timedelta(minutes=60), (1.1,) * 4)]
    g = Simulator(make_path(flat + late)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    assert g.reason is ExitReason.NOT_FILLED_NO_QUOTE and not g.counted


@pytest.mark.parametrize(("missing", "flag"), [(60, False), (61, True)])
def test_t9_7_path_gap(missing: int, flag: bool) -> None:
    rows = flat_rows(T0, 3)
    after = T0 + timedelta(minutes=3 + missing)
    rows += [minute(after, (1.1000, 1.1000, 1.0980, 1.0990))] + flat_rows(
        after + timedelta(minutes=1), 2, 1.099
    )
    f = Simulator(make_path(rows)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    assert f.reason is ExitReason.STOP and f.path_gap is flag and f.max_missing_minutes == missing


def test_t9_8_position_policy() -> None:
    rows = flat_rows(T0, 30) + [minute(T0 + timedelta(minutes=30), (1.1, 1.1, 1.098, 1.098))]
    rows += flat_rows(T0 + timedelta(minutes=31), 120, 1.098)
    sim = Simulator(make_path(rows))
    a = sim.simulate(spec(key="a"), Stream.EXEC, EXEC, COSTS)
    b = sim.simulate(spec(at=T0 + timedelta(minutes=10), key="b"), Stream.EXEC, EXEC, COSTS)
    c = sim.simulate(
        spec(Direction.SHORT, entry=1.098, at=T0 + timedelta(minutes=40), key="c"),
        Stream.EXEC,
        EXEC,
        COSTS,
    )
    executed, blocked = apply_position_policy([a, b, c])
    assert executed == [0, 2] and blocked == [1]


def test_t9_9_future_after_exit_does_not_matter() -> None:
    rows = flat_rows(T0, 2) + [minute(T0 + timedelta(minutes=2), (1.1, 1.1, 1.098, 1.098))]
    tail_a = flat_rows(T0 + timedelta(minutes=3), 50, 1.098)
    tail_b = [minute(T0 + timedelta(minutes=3 + k), (1.2, 1.3, 1.0, 1.2)) for k in range(50)]
    fa = Simulator(make_path(rows + tail_a)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    fb = Simulator(make_path(rows + tail_b)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    assert fa == fb


FILL_HASH = "980b8ea580a9dd54648231834a1e077dfa384d7588071fb91513e8967e517922"


def test_t9_10_deterministic_outcome_hash() -> None:
    rows = [
        minute(T0, (1.1000, 1.1002, 1.0998, 1.1001)),
        minute(T0 + timedelta(minutes=1), (1.1001, 1.1001, 1.0984, 1.0990)),
    ] + flat_rows(T0 + timedelta(minutes=2), 5, 1.0990)
    f = Simulator(make_path(rows)).simulate(spec(), Stream.EXEC, EXEC, COSTS)
    d = asdict(f)
    d["stream"], d["reason"] = f.stream.value, f.reason.value
    assert content_hash("Fill/1", d) == FILL_HASH


def test_path_import_roundtrip(tmp_path: Path) -> None:
    synthetic_raw_cache(tmp_path / "raw", "EURUSD", date(2024, 1, 8), date(2024, 1, 12))
    path, raw = import_path_dukascopy(
        RawCache(tmp_path / "raw"),
        "EURUSD",
        date(2024, 1, 8),
        date(2024, 1, 12),
        provenance=DatasetProvenance.SYNTHETIC,
    )
    assert len(raw["BID"]) == len(raw["ASK"]) == 5
    # Mon 00:00 UTC .. Fri 22:00 UTC is in session (Sun 22:00 open is before the range)
    assert len(path) == path.quality["in_session_minutes"] == (4 * 24 + 22) * 60
    assert (path.columns["ask_open"] >= path.columns["bid_open"]).all()
    write_path(tmp_path / "paths", path)
    again = load_path(tmp_path / "paths", path.path_id)
    assert again.path_id == path.path_id and (again.ordinal == path.ordinal).all()
    with pytest.raises(ValueError):
        path.columns["bid_open"][0] = 0.0  # read-only
    bad = {k: list(v) for k, v in path.columns.items()}
    bad["bid_high"][0] = bad["bid_low"][0] - 1.0
    with pytest.raises(PathIntegrityError):
        build_path("EURUSD", DatasetProvenance.SYNTHETIC, path_cov(path)[0], path_cov(path)[1], bad)


def path_cov(p: PricePath) -> tuple[datetime, datetime]:
    from fxnext.domain.timeutil import from_ns

    return from_ns(p.coverage_start_ns), from_ns(p.coverage_end_ns)


def test_fill_is_frozen() -> None:
    f = Fill("k", Stream.EXEC, ExitReason.TIME)
    with pytest.raises(AttributeError):
        f.key = "x"  # type: ignore[misc]
