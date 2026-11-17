"""T15: G10 carry and momentum (g10-cm-dev-001): data, portfolio, protocol."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from fxnext.data.daily import (
    FRED_SERIES,
    G10_DIGITS,
    DailyCache,
    DayCandle,
    RatesCache,
    decode_daily,
    download_daily,
    download_rates,
    encode_daily,
    parse_fred_csv,
)
from fxnext.data.dukascopy import HttpResult
from fxnext.domain.enums import PriceBasis
from fxnext.research.g10.panel import add_months, load_panel, split_window
from fxnext.research.g10.portfolio import Market, build_market, simulate, target_weights
from fxnext.research.g10.prereg import CURRENCIES, PAIRS, G10Costs, G10Variant, load_g10
from fxnext.research.g10.runner import block_bootstrap_mean_ci, run_g10
from fxnext.research.protocol import ProtocolError

PREREG = Path(__file__).resolve().parents[1] / "prereg" / "g10-cm-dev-001.toml"
START_PRICE = {
    "EURUSD": 1.2,
    "GBPUSD": 1.8,
    "AUDUSD": 0.75,
    "NZDUSD": 0.65,
    "USDJPY": 110.0,
    "USDCHF": 1.3,
    "USDCAD": 1.3,
    "USDNOK": 6.5,
    "USDSEK": 7.5,
}
RATES = {
    "USD": 2.0,
    "EUR": 2.5,
    "GBP": 4.0,
    "JPY": 0.1,
    "CHF": 0.5,
    "AUD": 5.5,
    "NZD": 6.0,
    "CAD": 3.0,
    "NOK": 3.5,
    "SEK": 1.5,
}


def _candles(
    symbol: str, year: int, rng: np.random.Generator, price: float, basis: str
) -> tuple[list[DayCandle], float]:
    out = []
    day = date(year, 1, 1)
    spread = 2e-4 * price
    while day.year == year:
        if day.weekday() >= 5:
            out.append(DayCandle(day, price, price, price, price, 0.0))
        else:
            new = price * float(np.exp(rng.normal(0.0, 0.006)))
            shift = spread / 2.0 if basis == "ASK" else -spread / 2.0
            o, c = price + shift, new + shift
            out.append(DayCandle(day, o, c, min(o, c), max(o, c), 1000.0))
            price = new
        day += timedelta(days=1)
    return out, price


def _write_cache(
    root: Path, last_year: int = 2013, extra_corrupt_year: int | None = None
) -> DailyCache:
    cache = DailyCache(root)
    for n, symbol in enumerate(PAIRS.values()):
        for basis in ("BID", "ASK"):
            rng = np.random.Generator(np.random.PCG64(1000 + n))  # same path for BID and ASK
            price = START_PRICE[symbol]
            for year in range(2003, last_year + 1):
                candles, price = _candles(symbol, year, rng, price, basis)
                payload = encode_daily(candles, G10_DIGITS[symbol])
                path = cache.store(symbol, PriceBasis(basis), year, payload)
                cache.append(
                    {
                        "symbol": symbol,
                        "basis": basis,
                        "year": year,
                        "status": "OK",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "cache_path": path.relative_to(root).as_posix(),
                    }
                )
            if extra_corrupt_year is not None:
                cache.append(
                    {
                        "symbol": symbol,
                        "basis": basis,
                        "year": extra_corrupt_year,
                        "status": "OK",
                        "sha256": "0" * 64,
                        "cache_path": "does/not/exist.bi5",
                    }
                )
    return cache


def _write_rates(
    root: Path, overrides: dict[tuple[str, tuple[int, int]], str] | None = None
) -> None:
    overrides = overrides or {}
    root.mkdir(parents=True, exist_ok=True)
    for ccy, sid in FRED_SERIES.items():
        lines = [f"observation_date,{sid}"]
        for y in range(2002, 2027):
            for m in range(1, 13):
                lines.append(f"{y}-{m:02d}-01,{overrides.get((ccy, (y, m)), f'{RATES[ccy]:.2f}')}")
        payload = ("\n".join(lines) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        (root / f"{sid}.{digest[:12]}.csv").write_bytes(payload)


def _small_prereg(tmp: Path, **changes: str) -> Path:
    text = PREREG.read_text()
    text = text.replace("benchmark_repetitions = 1000", "benchmark_repetitions = 60")
    text = text.replace("bootstrap_samples = 10000", "bootstrap_samples = 300")
    for old, new in changes.items():
        text = text.replace(old, new)
    out = tmp / "g10.toml"
    out.write_text(text)
    return out


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("g10")
    _write_cache(root / "raw-d1", extra_corrupt_year=2014)
    _write_rates(root / "rates")
    return root / "raw-d1", root / "rates"


def test_daily_codec_roundtrip() -> None:
    candles = [
        DayCandle(date(2010, 1, 1), 93.01, 93.0, 92.98, 93.02, 4811.0),
        DayCandle(date(2010, 1, 4), 92.735, 92.47, 92.185, 93.21, 578017.0),
    ]
    assert decode_daily(encode_daily(candles, 3), 2010, 3) == tuple(candles)


def test_download_daily_logs_and_skips_unfinished_years(tmp_path: Path) -> None:
    payload = encode_daily([DayCandle(date(2004, 1, 2), 1.2, 1.21, 1.19, 1.22, 5.0)], 5)
    calls: list[str] = []

    def fetcher(url: str, timeout: float) -> HttpResult:
        calls.append(url)
        return HttpResult(404, b"") if "2005" in url else HttpResult(200, payload)

    cache = DailyCache(tmp_path)
    counts = download_daily(
        cache,
        ["EURUSD"],
        [PriceBasis.BID],
        2004,
        2026,
        fetcher=fetcher,
        now=lambda: datetime(2026, 11, 17, tzinfo=UTC),
        sleep=lambda _s: None,
    )
    assert counts["ok"] == 21 and counts["not_found"] == 1
    assert not any("/2026/" in u for u in calls)  # the current year is never cached
    assert calls[0].endswith("/EURUSD/2004/BID_candles_day_1.bi5")
    again = download_daily(
        cache,
        ["EURUSD"],
        [PriceBasis.BID],
        2004,
        2006,
        fetcher=fetcher,
        now=lambda: datetime(2026, 11, 17, tzinfo=UTC),
        sleep=lambda _s: None,
    )
    assert again["skipped"] == 3 and again["requested"] == 0


def test_fred_csv_parse_and_download(tmp_path: Path) -> None:
    body = b"observation_date,IR3TIB01USM156N\n2003-01-01,1.29\n2003-02-01,.\n"
    assert parse_fred_csv(body, "IR3TIB01USM156N") == {(2003, 1): 1.29, (2003, 2): None}
    with pytest.raises(ValueError):
        parse_fred_csv(body, "IR3TIB01EZM156N")
    statuses = download_rates(
        RatesCache(tmp_path), ["IR3TIB01USM156N"], fetcher=lambda u, t: HttpResult(200, body)
    )
    assert statuses == {"IR3TIB01USM156N": "OK"}
    files = list(tmp_path.glob("IR3TIB01USM156N.*.csv"))
    assert len(files) == 1 and files[0].read_bytes() == body


def test_splits_and_holdout_closed(tmp_path: Path) -> None:
    assert split_window("development") == ((2004, 1), (2013, 12), 2013)
    with pytest.raises(ProtocolError):
        split_window("holdout")
    with pytest.raises(ProtocolError):  # validation without a recorded approval
        load_g10(_small_prereg(tmp_path, **{'split = "development"': 'split = "validation"'}))


def test_prereg_matches_frozen_text() -> None:
    pre, _ = load_g10(PREREG)
    assert [v.name for v in pre.variants] == ["C2", "C3", "M3", "M6", "M12", "CM3", "CM12"]
    assert [(v.k, v.lookback_months) for v in pre.variants] == [
        (2, None),
        (3, None),
        (3, 3),
        (3, 6),
        (3, 12),
        (3, 3),
        (3, 12),
    ]
    assert pre.costs.markup_pct_per_year == 1.0 and pre.rate_lag_months == 1
    assert (pre.benchmark_seed, pre.bootstrap_seed) == (20261103, 20261104)


def test_panel_reads_only_the_split(data: tuple[Path, Path]) -> None:
    raw, rates = data
    panel = load_panel(raw, rates, "development")  # the 2014 record points to no file
    assert panel.days[-1] == date(2013, 12, 31)
    assert max(panel.rebalance) == (2013, 12)
    assert max(panel.rates["USD"]) == (2013, 12)
    assert panel.quality["months_without_rebalance"] == []
    assert panel.days[panel.rebalance[(2010, 4)]] == date(2010, 4, 30)


def test_carry_ranks_and_costs(data: tuple[Path, Path]) -> None:
    raw, rates = data
    panel = load_panel(raw, rates, "development")
    costs = G10Costs()
    market = build_market(panel, (2004, 1), (2013, 12), costs, 1)
    assert len(market.months) == 120 and market.months[0] == (2004, 1)
    w, active, reasons = target_weights(panel, market, G10Variant(name="C2", signal="carry", k=2))
    assert active.all() and not reasons
    longs = {CURRENCIES[c] for c in np.flatnonzero(w[0] > 0)}
    shorts = {CURRENCIES[c] for c in np.flatnonzero(w[0] < 0)}
    assert longs == {"NZD", "AUD"} and shorts == {"JPY", "CHF"}
    ret = simulate(market, w, costs)
    # constant weights: full build in the first month, then only the drift is traded
    assert ret.turnover[0] == pytest.approx(2.0)
    assert ret.turnover[1:].max() < 0.2
    # carry income: (6.0 + 5.5 - 0.1 - 0.5) / 2 % a year
    expected = (6.0 + 5.5 - 0.1 - 0.5) / 2 / 100 * market.days / 360
    assert np.allclose(ret.carry, expected)
    assert np.allclose(ret.markup, 2 * 0.01 * market.days / 360)
    # spread cost: half spread of about 1 bp relative on 2 units traded
    assert ret.spread[0] == pytest.approx(2 * 1e-4, rel=0.25)
    assert np.allclose(ret.net(2.0), ret.gross - ret.spread - 2 * ret.fees - ret.markup)


def test_usd_can_be_ranked_and_has_no_exposure() -> None:
    market = Market(
        months=[(2004, 1), (2004, 2)],
        start_month=[(2003, 12), (2004, 1)],
        contiguous=np.array([True, True]),
        days=np.array([31.0, 29.0]),
        fx=np.tile(np.linspace(-0.01, 0.01, 10), (2, 1)),
        carry=np.zeros((2, 10)),
        half_spread=np.full((2, 10), 1e-4),
        fee_rel=np.full((2, 10), 1e-5),
        eligible=np.ones((2, 10), dtype=bool),
        rate_at_open=np.full((2, 10), 1.0),
        rebal_rows=[0, 1],
    )
    w = np.zeros((2, 10))
    w[:, 0] = 0.5  # USD long
    w[:, 1] = 0.5
    w[:, 2] = -1.0
    ret = simulate(market, w, G10Costs())
    assert ret.gross[0] == pytest.approx(0.5 * market.fx[0, 1] - market.fx[0, 2])
    assert ret.turnover[0] == pytest.approx(1.5)


def test_rates_lag_and_missing_rate_excludes_currency(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _write_cache(raw, last_year=2005)
    # NZD rate falls to 0 from June 2004 and is missing in September 2004
    over = {("NZD", (2004, m)): "0.00" for m in range(6, 13)}
    over[("NZD", (2004, 9))] = "."
    _write_rates(tmp_path / "rates", over)
    import fxnext.research.g10.panel as panel_mod

    old = panel_mod.G10_SPLITS
    try:
        panel_mod.G10_SPLITS = {**old, "development": (date(2004, 1, 1), date(2006, 1, 1))}
        panel = load_panel(raw, tmp_path / "rates", "development")
    finally:
        panel_mod.G10_SPLITS = old
    market = build_market(panel, (2004, 1), (2005, 12), G10Costs(), 1)
    w, active, _ = target_weights(panel, market, G10Variant(name="C2", signal="carry", k=2))
    nzd = CURRENCIES.index("NZD")
    by_open = {market.start_month[j]: w[j, nzd] for j in range(len(market.months))}
    assert by_open[(2004, 6)] > 0  # end of June still uses the May rate
    assert by_open[(2004, 7)] < 0  # end of July uses the June rate (0.00)
    assert by_open[(2004, 10)] == 0  # end of October: September missing, NZD excluded
    assert not market.eligible[[m == (2004, 10) for m in market.start_month], nzd].any()
    assert active.all()


def test_no_look_ahead_in_signals(tmp_path: Path) -> None:
    raw_a, raw_b = tmp_path / "a", tmp_path / "b"
    _write_cache(raw_a)
    _write_cache(raw_b, last_year=2012)
    # b: 2013 drawn with a different random path
    cache = DailyCache(raw_b)
    for n, symbol in enumerate(PAIRS.values()):
        for basis in ("BID", "ASK"):
            rng = np.random.Generator(np.random.PCG64(9000 + n))
            candles, _ = _candles(symbol, 2013, rng, START_PRICE[symbol], basis)
            payload = encode_daily(candles, G10_DIGITS[symbol])
            path = cache.store(symbol, PriceBasis(basis), 2013, payload)
            cache.append(
                {
                    "symbol": symbol,
                    "basis": basis,
                    "year": 2013,
                    "status": "OK",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "cache_path": path.relative_to(raw_b).as_posix(),
                }
            )
    _write_rates(tmp_path / "rates")
    out = []
    for raw in (raw_a, raw_b):
        panel = load_panel(raw, tmp_path / "rates", "development")
        market = build_market(panel, (2004, 1), (2013, 12), G10Costs(), 1)
        w, _, _ = target_weights(
            panel, market, G10Variant(name="M3", signal="momentum", k=3, lookback_months=3)
        )
        out.append((market, w, simulate(market, w, G10Costs())))
    (ma, wa, ra), (mb, wb, rb) = out
    cut = ma.months.index((2012, 12)) + 1  # periods ending by December 2012
    assert np.array_equal(wa[:cut], wb[:cut])
    assert np.allclose(ra.net()[:cut], rb.net()[:cut])
    assert not np.allclose(ra.net()[cut:], rb.net()[cut:])


def test_block_bootstrap_is_seeded() -> None:
    x = np.random.Generator(np.random.PCG64(1)).normal(0.01, 0.02, 120)
    a = block_bootstrap_mean_ci(x, block=3, samples=500, seed=20261104)
    assert a == block_bootstrap_mean_ci(x, block=3, samples=500, seed=20261104)
    assert a is not None and a[0] < x.mean() < a[1]


def test_end_to_end_run(data: tuple[Path, Path], tmp_path: Path) -> None:
    raw, rates = data
    pre = _small_prereg(tmp_path)
    res = run_g10(
        pre,
        cache_root=raw,
        rates_root=rates,
        research_root=tmp_path / "research",
        code_version="test",
    )
    r = res.report
    assert r["periods"] == 120
    assert set(r["variants"]) == {"C2", "C3", "M3", "M6", "M12", "CM3", "CM12"}
    assert r["variants"]["C2"]["net"]["months"] == 120
    # M12 at the end of December 2003 needs December 2002, before the warm-up data
    assert r["variants"]["M12"]["net"]["months"] == 119
    assert r["assessment"]["outcome"] in ("PROPOSE_VALIDATION", "CLOSE_ON_DEV")
    assert set(r["assessment"]["checks"]) == {
        "best_net_positive",
        "best_above_max_of_k_p95",
        "positive_variants",
        "positive_subperiods",
    }
    assert (res.path / "report.md").read_text().startswith("# g10-cm-dev-001")
    assert sum(1 for _ in (res.path / "monthly.csv").open()) == 1 + 7 * 120
    again = run_g10(
        pre,
        cache_root=raw,
        rates_root=rates,
        research_root=tmp_path / "research",
        code_version="test",
    )
    assert again.fingerprint == res.fingerprint
    assert json.loads((res.path / "report.json").read_text())["fingerprint"] == res.fingerprint
    assert add_months((2004, 1), -1) == (2003, 12)
