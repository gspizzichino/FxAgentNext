"""T14: hypotheses S0-S3 and S7 (bars, sessions, rules, look-ahead, swap, protocol)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from fxnext.data.path import PricePath, build_path, write_path
from fxnext.domain.calendar import FxCalendar, ny_1700_utc
from fxnext.domain.enums import DatasetProvenance, Direction, Timeframe
from fxnext.domain.timeutil import from_ns, to_ns
from fxnext.research.execution import (
    CostModel,
    ExecParams,
    ExitReason,
    Simulator,
    Stream,
    TradeSpec,
)
from fxnext.research.hyp import bench
from fxnext.research.hyp.bars import (
    MIN_NS,
    atr,
    build_bars,
    london_window,
    prior_max,
)
from fxnext.research.hyp.desc import (
    rw_closure_probability,
    s3_phase1,
    s7_phase1,
    session_returns,
    variance_ratio,
    weekends,
)
from fxnext.research.hyp.prereg import load_hypothesis
from fxnext.research.hyp.runner import run_hypothesis
from fxnext.research.hyp.strategies import RULES, Context
from fxnext.research.hyp.swap import policy_rate, swap_price
from fxnext.research.protocol import ProtocolError

START = datetime(2015, 1, 1, tzinfo=UTC)
PREREG = Path(__file__).resolve().parents[1] / "prereg"
SYMBOLS = (("EURUSD", 1.2, 1e-4), ("GBPUSD", 1.5, 1e-4), ("USDJPY", 110.0, 1e-2))


def make_path(
    symbol: str,
    p0: float,
    pip: float,
    end: datetime,
    *,
    seed: int = 0,
    missing: float = 0.0,
    weekend_gap_pips: float = 0.0,
    vol: float = 1.5,
) -> PricePath:
    t = FxCalendar().slot_grid(Timeframe.M1, START, end)
    rng = np.random.default_rng(seed)
    t = t[rng.random(t.size) >= missing]
    steps = rng.normal(0, vol * pip, t.size)
    if weekend_gap_pips:
        jumps = np.flatnonzero(np.diff(t) > 3600 * 10**9) + 1
        steps[jumps] += weekend_gap_pips * pip * rng.choice([-1.0, 1.0], jumps.size)
    close = p0 + np.cumsum(steps)
    open_ = np.r_[p0, close[:-1]]
    if weekend_gap_pips:
        open_[jumps] = close[jumps]
    hi = np.maximum(open_, close) + np.abs(rng.normal(0, 0.3 * pip, t.size))
    lo = np.minimum(open_, close) - np.abs(rng.normal(0, 0.3 * pip, t.size))
    sp = 0.8 * pip
    cols = {
        "t_ns": t,
        "status": np.zeros(t.size, np.uint8),
        "bid_open": open_,
        "bid_high": hi,
        "bid_low": lo,
        "bid_close": close,
        "ask_open": open_ + sp,
        "ask_high": hi + sp,
        "ask_low": lo + sp,
        "ask_close": close + sp,
    }
    return build_path(symbol, DatasetProvenance.SYNTHETIC, START, end, cols)


def perturb_after(path: PricePath, cut_ns: int, factor: float = 1.01) -> PricePath:
    cols = {k: np.array(v) for k, v in path.columns.items()}
    after = cols["t_ns"] >= cut_ns
    for k in cols:
        if k.endswith(("open", "high", "low", "close")):
            cols[k][after] *= factor
    return build_path(
        path.symbol,
        path.provenance,
        from_ns(path.coverage_start_ns),
        from_ns(path.coverage_end_ns),
        cols,  # type: ignore[arg-type]
    )


END = datetime(2015, 5, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def eurusd() -> PricePath:
    return make_path("EURUSD", 1.2, 1e-4, END, seed=1)


# --------------------------------------------------------------------- bars, time


def test_d1_bars_close_at_ny_1700_without_sunday_stub(eurusd: PricePath) -> None:
    d1 = build_bars(eurusd, "D1", "BID")
    for t in d1.open_ns[:20].tolist():
        open_ = from_ns(t)
        assert open_ == ny_1700_utc((open_ - timedelta(hours=12)).date())
        assert open_.weekday() in (6, 0, 1, 2, 3)  # Sun..Thu 17:00 NY opens
    weeks = {from_ns(t).isocalendar()[1] for t in d1.open_ns.tolist()}
    assert len(d1) <= 5 * (len(weeks) + 1)
    i = 10
    t = eurusd.columns["t_ns"]
    m = (t >= d1.open_ns[i]) & (t < d1.close_ns[i])
    assert d1.h[i] == eurusd.columns["bid_high"][m].max()
    assert d1.lo[i] == eurusd.columns["bid_low"][m].min()
    assert d1.c[i] == eurusd.columns["bid_close"][m][-1]
    assert d1.minutes[i] == 1440 and d1.complete[i]


def test_london_window_follows_uk_summer_time() -> None:
    w0, _ = london_window(date(2015, 1, 5), 7, 12)
    assert from_ns(w0) == datetime(2015, 1, 5, 7, tzinfo=UTC)
    w0, w1 = london_window(date(2015, 3, 30), 7, 12)
    assert from_ns(w0) == datetime(2015, 3, 30, 6, tzinfo=UTC)
    assert (w1 - w0) == 5 * 3600 * 10**9


def test_indicators_use_only_past_bars() -> None:
    x = np.arange(30, dtype=float)
    pm = prior_max(x, 5)
    assert math.isnan(pm[4]) and pm[5] == 4.0 and pm[29] == 28.0


# ---------------------------------------------------------------------------- S0


def test_variance_ratio_detects_persistence_and_iid() -> None:
    rng = np.random.default_rng(3)
    iid = rng.normal(size=20000)
    seg = np.zeros(iid.size, np.int64)
    cell = variance_ratio(iid, seg, 4)
    assert cell["status"] == "ok" and abs(cell["vr"] - 1) < 0.05
    assert cell["label"] == "nessun segnale"
    ar = np.empty(20000)
    ar[0] = 0.0
    e = rng.normal(size=20000)
    for i in range(1, ar.size):
        ar[i] = 0.3 * ar[i - 1] + e[i]
    cell = variance_ratio(ar, seg, 4)
    assert cell["vr"] > 1.3 and cell["z"] > 2.58 and cell["label"] == "persistenza"
    # q larger than any segment cannot be formed
    short = variance_ratio(iid[:100], np.arange(100) // 5, 8)
    assert short["status"] == "non calcolabile"


# ---------------------------------------------------------------------------- S7


def test_s7_session_returns_and_gate(eurusd: PricePath) -> None:
    start, end = to_ns(START), eurusd.coverage_end_ns
    rets, excluded = session_returns(eurusd, start, end)
    assert rets and not excluded.get("london_incomplete")
    r = rets[5]
    t = eurusd.columns["t_ns"]
    i0, i1 = np.searchsorted(t, r.start_ns), np.searchsorted(t, r.end_ns)
    mid_o = (eurusd.columns["bid_open"][i0] + eurusd.columns["ask_open"][i0]) / 2
    mid_c = (eurusd.columns["bid_close"][i1 - 1] + eurusd.columns["ask_close"][i1 - 1]) / 2
    assert r.log_return == pytest.approx(math.log(mid_c / mid_o))
    out = s7_phase1(rets)
    assert set(out["legs"]) == {"home", "ny"}
    assert out["phase2_variants"] == []  # a random walk shows no session effect


def test_s7_candidates_fixed_directions(eurusd: PricePath) -> None:
    ctx = Context(eurusd, to_ns(START), eurusd.coverage_end_ns)
    pre, _ = load_hypothesis(PREREG / "s7-intraday-dev-001.toml")
    c = pre.variants[2]  # both legs
    cands = RULES["s7_intraday"].candidates(ctx, c)
    sessions = {m.extra["session"] for _, m in cands}
    assert sessions == {"london", "ny"}
    for spec, meta in cands:
        expected = Direction.SHORT if meta.extra["session"] == "london" else Direction.LONG
        assert spec.direction is expected
        assert spec.risk == pytest.approx(meta.atr)
        assert spec.exit_at_ns is not None and spec.exit_at_ns > spec.expires_at_ns


# ---------------------------------------------------------------------------- S3


def test_s3_weekends_gap_and_rules() -> None:
    path = make_path("EURUSD", 1.2, 1e-4, END, seed=5, weekend_gap_pips=40)
    ws = weekends(path, to_ns(START), path.coverage_end_ns)
    ok = [w for w in ws if w.status == "ok"]
    assert ok and all(abs(abs(w.gap) / 1e-4 - 40) < 10 for w in ok)
    assert {w.status for w in ws} <= {"ok", "no_daily_history", "not_a_weekend"}
    p1 = s3_phase1(ws, (0.25, 0.5))
    assert p1["by_k"]["k0.25"]["pooled"]["weekends"] > 0
    ctx = Context(path, to_ns(START), path.coverage_end_ns)
    pre, _ = load_hypothesis(PREREG / "s3-sungap-dev-001.toml")
    for v in pre.variants:
        for spec, meta in RULES["s3_sunday_gap"].candidates(ctx, v):
            assert spec.target == meta.extra["friday_close"]
            sign = 1.0 if spec.direction is Direction.LONG else -1.0
            assert sign * (spec.target - spec.entry_ref) > 0
            assert spec.exit_at_ns == spec.available_at_ns + 48 * 3600 * 10**9


def test_random_walk_closure_probability() -> None:
    assert rw_closure_probability(0.0, 1.0) == pytest.approx(1.0)
    assert rw_closure_probability(1.0, 1.0) == pytest.approx(2 * (1 - 0.7602499389), abs=1e-6)


# -------------------------------------------------------------------- look-ahead


@pytest.mark.parametrize("kind", ["s1_failure_test", "s2_daily_trend", "s7_intraday"])
def test_rules_do_not_look_ahead(kind: str) -> None:
    path = make_path("EURUSD", 1.2, 1e-4, datetime(2015, 7, 1, tzinfo=UTC), seed=7, vol=3.0)
    cut = to_ns(datetime(2015, 5, 13, 12, tzinfo=UTC))
    future = perturb_after(path, cut)
    name = {
        "s1_failure_test": "s1-failure",
        "s2_daily_trend": "s2-dtrend",
        "s7_intraday": "s7-intraday",
    }
    pre, _ = load_hypothesis(PREREG / f"{name[kind]}-dev-001.toml")
    rule = RULES[kind]
    for v in pre.variants[:4]:
        a = rule.candidates(Context(path, to_ns(START), path.coverage_end_ns), v)
        b = rule.candidates(Context(future, to_ns(START), future.coverage_end_ns), v)
        past_a = [(s.key, s.entry_ref, s.stop, s.target) for s, _ in a if s.available_at_ns <= cut]
        past_b = [(s.key, s.entry_ref, s.stop, s.target) for s, _ in b if s.available_at_ns <= cut]
        assert past_a and past_a == past_b


def test_s1_geometry() -> None:
    path = make_path("EURUSD", 1.2, 1e-4, END, seed=2, vol=3.0)
    ctx = Context(path, to_ns(START), path.coverage_end_ns)
    pre, _ = load_hypothesis(PREREG / "s1-failure-dev-001.toml")
    for v in pre.variants:
        cands = RULES["s1_failure_test"].candidates(ctx, v)
        assert cands
        assert len({s.key for s, _ in cands}) == len(cands)
        for spec, meta in cands:
            sign = 1.0 if spec.direction is Direction.LONG else -1.0
            assert sign * (spec.entry_ref - spec.stop) >= 0.5 * meta.atr - 1e-12
            assert spec.target == pytest.approx(spec.entry_ref + sign * v.k * spec.risk)  # type: ignore[operator]
    w0 = RULES["s1_failure_test"].candidates(ctx, pre.variants[0])
    overlap = RULES["s1_failure_test"].baseline_overlap(ctx, pre.variants[0], w0)  # type: ignore[attr-defined]
    assert overlap["became_failure_test"] == 0  # w = 0 never sees a close-beyond breakout


# --------------------------------------------------------------- simulator, swap


def test_scheduled_exit_uses_last_close_before_exit_at(eurusd: PricePath) -> None:
    t = eurusd.columns["t_ns"]
    i = 5000
    at = int(t[i])
    spec = TradeSpec(
        "x",
        "EURUSD",
        Direction.LONG,
        1.2,
        -math.inf,
        math.inf,
        at,
        at + 15 * MIN_NS,
        at + 90 * MIN_NS,
        unit_risk=0.001,
    )
    f = Simulator(eurusd).simulate(spec, Stream.SIGNAL, ExecParams(), CostModel())
    assert f.reason is ExitReason.TIME
    j = int(np.searchsorted(t, at + 90 * MIN_NS)) - 1
    assert f.exit_ns == int(t[j]) and f.exit_price == eurusd.columns["bid_close"][j]
    with pytest.raises(ValueError, match="scheduled exit"):
        Simulator(eurusd).simulate(
            replace(spec, exit_at_ns=at), Stream.SIGNAL, ExecParams(), CostModel()
        )


def test_swap_signs_and_wednesday() -> None:
    days = [date(2016, 6, 6), date(2016, 6, 8)]  # Monday, Wednesday (x3)
    amount, nights = swap_price("EURUSD", True, 1.1, days, 1.0)
    assert nights == 4
    expected = 1.1 * ((-0.40 - 0.50) - 1.0) / 100 / 365 * 4
    assert amount == pytest.approx(expected)
    short, _ = swap_price("EURUSD", False, 1.1, days, 1.0)
    assert short == pytest.approx(1.1 * ((0.90) - 1.0) / 100 / 365 * 4)
    assert policy_rate("JPY", date(2016, 2, 15)) == 0.10
    assert policy_rate("JPY", date(2016, 2, 16)) == -0.10
    with pytest.raises(ValueError, match="does not cover"):
        policy_rate("USD", date(2021, 1, 4))


def test_max_of_k_and_atr() -> None:
    a = np.array([0.1, -0.2, np.nan])
    b = np.array([-0.1, 0.3, np.nan])
    out = bench.max_of_k([a, b])
    assert out[0] == 0.1 and out[1] == 0.3 and math.isnan(out[2])
    path = make_path("EURUSD", 1.2, 1e-4, datetime(2015, 2, 1, tzinfo=UTC), seed=4)
    h1 = build_bars(path, "H1", "BID")
    a14 = atr(h1, 14)
    assert np.isnan(a14[:14]).all() and np.isfinite(a14[20:]).all()


# ---------------------------------------------------------------------- protocol


def test_preregistrations_match_the_frozen_text() -> None:
    files = sorted(PREREG.glob("*.toml"))
    assert [f.name for f in files] == [
        "s0-vr-dev-001.toml",
        "s1-failure-dev-001.toml",
        "s2-dtrend-dev-001.toml",
        "s3-sungap-dev-001.toml",
        "s7-intraday-dev-001.toml",
    ]
    loaded = {f.stem: load_hypothesis(f)[0] for f in files}
    for pre in loaded.values():
        assert pre.split == "development"
        assert pre.frozen_text_sha256.startswith("589bd00a")
        assert [d.symbol for d in pre.data] == ["EURUSD", "GBPUSD", "USDJPY"]
        assert pre.cost_multipliers == (1.0, 1.5, 2.0)
        assert (pre.benchmark_repetitions, pre.benchmark_seed) == (1000, 20261103)
    s1 = loaded["s1-failure-dev-001"]
    assert len(s1.variants) == 8 and s1.threshold.entry_test_min_win_rate == 0.52
    assert {(v.tf, v.w, v.k) for v in s1.variants} == {
        (tf, w, k) for tf in ("M15", "H1") for w in (0, 1) for k in (1.5, 2.0)
    }
    s2 = loaded["s2-dtrend-dev-001"]
    assert s2.exploratory and s2.main_variant == "N55-s2-M20" and len(s2.variants) == 8
    assert s2.swap is not None and s2.swap.markup_pct_per_year == 1.0
    s3 = loaded["s3-sungap-dev-001"]
    assert s3.exploratory and len(s3.variants) == 8 and s3.phase1_ks == (0.25, 0.5)
    assert [v.legs for v in loaded["s7-intraday-dev-001"].variants] == [
        ("home",),
        ("ny",),
        ("home", "ny"),
    ]
    assert not loaded["s0-vr-dev-001"].variants


def test_prereg_refuses_other_splits_and_swap_elsewhere(tmp_path: Path) -> None:
    text = (PREREG / "s7-intraday-dev-001.toml").read_text()
    bad = tmp_path / "bad.toml"
    bad.write_text(text.replace('split = "development"', 'split = "validation"'))
    with pytest.raises(ValueError):
        load_hypothesis(bad)
    bad.write_text(
        text.replace(
            "exploratory = false", "exploratory = false\n[swap]\nmarkup_pct_per_year = 1.0\n", 1
        )
    )
    with pytest.raises(ProtocolError, match="S2 only"):
        load_hypothesis(bad)


def _small_prereg(tmp_path: Path, name: str, ids: dict[str, str], keep: int, reps: int) -> Path:
    text = (PREREG / name).read_text()
    for sym, new in ids.items():
        old = text.split(f'symbol = "{sym}"\npath = "')[1][:64]
        text = text.replace(old, new)
    head, *variants = text.split("[[variants]]")
    text = head + "".join("[[variants]]" + v for v in variants[:keep])
    text = text.replace(
        "exploratory = ",
        f"benchmark_repetitions = {reps}\nbootstrap_samples = 200\nexploratory = ",
        1,
    )
    out = tmp_path / name
    out.write_text(text)
    return out


def test_end_to_end_on_synthetic_paths(tmp_path: Path) -> None:
    root = tmp_path / "paths"
    end = datetime(2015, 4, 1, tzinfo=UTC)
    ids = {}
    for k, (sym, p0, pip) in enumerate(SYMBOLS):
        p = make_path(sym, p0, pip, end, seed=10 + k, vol=3.0)
        write_path(root, p)
        ids[sym] = p.path_id
    for name, keep in (
        ("s1-failure-dev-001.toml", 8),
        ("s7-intraday-dev-001.toml", 3),
        ("s0-vr-dev-001.toml", 0),
    ):
        pre = _small_prereg(tmp_path, name, ids, keep, reps=20)
        if name.startswith("s1"):  # H1 variants only, to keep the test fast
            text = pre.read_text()
            head, *vs = text.split("[[variants]]")
            pre.write_text(head + "".join("[[variants]]" + v for v in vs if '"H1"' in v))
        res = run_hypothesis(
            pre,
            paths_root=root,
            research_root=tmp_path / "res",
            code_version="t",
            force_phase2=True,
        )
        again = run_hypothesis(
            pre,
            paths_root=root,
            research_root=tmp_path / "res",
            code_version="t",
            force_phase2=True,
        )
        assert again.fingerprint == res.fingerprint
        assert (res.path / "report.md").exists()
        r = res.report
        assert "phase 2 forced for testing: not a valid research result" in r[
            "warnings"
        ] or name.startswith("s0")
        for v in r.get("variants", {}).values():
            for s in v["per_symbol"].values():
                assert s["reconciliation"]["balanced"]
            assert v["benchmarks"]["random_direction"]["repetitions"] == 20
        if name.startswith("s1"):
            a = r["assessment"]
            assert a["max_of_k"]["random_direction"]["k"] == 4
            assert set(a["checks"]) >= {"net_positive_x1", "plateau", "entry_test_best_variant"}
        if name.startswith("s0"):
            assert r["s0"]["EURUSD"]["H1"]["all"]["q4"]["status"] == "ok"
