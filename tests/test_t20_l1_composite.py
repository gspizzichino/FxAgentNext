"""T20: l1-composito-001 (M10): sources, signals, portfolio, single run."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest

from fxnext.data.daily import RatesCache
from fxnext.data.dukascopy import HttpResult
from fxnext.research.composite.model import (
    Known,
    build_panel,
    centered_ranks,
    month_end_log_spot,
    weights_from,
)
from fxnext.research.composite.runner import load_l1, run_l1
from fxnext.research.composite.sources import (
    CURRENCIES,
    DEM_PER_EUR,
    DEM_SERIES,
    FX_SERIES,
    MACRO_ORDER,
    RATE_SERIES,
    VARIABLES,
    Series,
    all_fred_series,
    download_sources,
    macro_candidates,
    parse_bundesbank,
    parse_fred,
)
from fxnext.research.protocol import ProtocolError

PREREG = Path(__file__).resolve().parents[1] / "prereg" / "l1-composito-001.toml"
FROZEN = Path(__file__).resolve().parents[1] / "docs" / "M10-prereg-l1-composito-001-congelato.md"


def _fred_csv(sid: str, rows: list[tuple[date, float | None]]) -> bytes:
    lines = [f"observation_date,{sid}"]
    lines += [f"{d.isoformat()},{'' if v is None else repr(v)}" for d, v in rows]
    return ("\n".join(lines) + "\n").encode()


def _bundesbank_csv(rows: list[tuple[date, float | None]]) -> bytes:
    head = [f'"",{DEM_SERIES},{DEM_SERIES}_FLAGS', '"",Exchange rates on the Frankfurt exchange,']
    head += ["Decimals,4,", "Time format code,P1D,"]
    body = [f"{d.isoformat()},{'.' if v is None else v}," for d, v in rows]
    return ("\n".join(head + body) + "\n").encode()


def _month_starts(y0: int, y1: int, step: int = 1) -> list[date]:
    return [date(y, m, 1) for y in range(y0, y1 + 1) for m in range(1, 13, step)]


def _month_ends(y0: int, y1: int) -> list[date]:
    return [date(y, m, 27) for y in range(y0, y1 + 1) for m in range(1, 13)]


def _write_world(root: Path, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    cache = RatesCache(root)
    for ccy, (sid, _usd_per) in FX_SERIES.items():
        y0 = 1999 if ccy == "EUR" else 1971
        days = _month_ends(y0, 2003)
        walk = np.exp(np.cumsum(rng.normal(0, 0.03, len(days))))
        cache.store(sid, _fred_csv(sid, [(d, float(v)) for d, v in zip(days, walk, strict=True)]))
    dem_days = _month_ends(1971, 1998)
    dem = 2.0 * np.exp(np.cumsum(rng.normal(0, 0.03, len(dem_days))))
    cache.store(
        DEM_SERIES, _bundesbank_csv([(d, float(v)) for d, v in zip(dem_days, dem, strict=True)])
    )
    for ccy in MACRO_ORDER:
        for var in VARIABLES:
            sid = macro_candidates(ccy, var)[0]
            starts = _month_starts(1970, 2003)
            level = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, len(starts))))
            cache.store(
                sid, _fred_csv(sid, [(d, float(v)) for d, v in zip(starts, level, strict=True)])
            )
        for sid, _ in RATE_SERIES[ccy]:
            starts = _month_starts(1970, 2003)
            cache.store(sid, _fred_csv(sid, [(d, 5.0 + rng.normal()) for d in starts]))


def test_frozen_text_matches_prereg() -> None:
    import hashlib

    pre, _ = load_l1(PREREG)
    assert hashlib.sha256(FROZEN.read_bytes()).hexdigest() == pre.frozen_text_sha256
    assert pre.t_threshold == 2.0 and pre.min_currencies == 7


def test_centered_ranks_and_weights() -> None:
    r = centered_ranks({0: 3.0, 1: 1.0, 2: 2.0})
    assert r == {1: -1.0, 2: 0.0, 0: 1.0}
    assert centered_ranks({0: 1.0, 1: 1.0}) == {0: 0.0, 1: 0.0}
    w = weights_from({c: float(c) for c in range(9)}, 2)
    assert math.isclose(w[w > 0].sum(), 1.0) and math.isclose(w[w < 0].sum(), -1.0)
    assert w[8] > w[5] > 0 > w[0]
    assert not weights_from({0: 1.0}, 2).any()


def test_publication_lags() -> None:
    monthly = Known({(2000, 1): 100.0, (1999, 1): 90.0}, lag=2)
    assert monthly.latest((2000, 2)) is None or monthly.latest((2000, 2)) != (2000, 1)
    assert monthly.latest((2000, 3)) == (2000, 1)
    assert math.isclose(monthly.log_growth((2000, 3), 12) or 0, math.log(100 / 90))
    quarterly = Known({(2000, 1): 1.0, (2000, 4): 2.0}, lag=5)
    assert quarterly.latest((2000, 5)) is None
    assert quarterly.latest((2000, 6)) == (2000, 1)  # Q1 known at the end of Q2
    assert quarterly.latest((2000, 9)) == (2000, 4)


def test_parsers_and_frequency() -> None:
    s = parse_fred(_fred_csv("X", [(date(1990, 1, 1), 1.0), (date(1990, 4, 1), None)]), "X")
    assert s.obs == {date(1990, 1, 1): 1.0}
    q = parse_fred(_fred_csv("Q", [(d, 1.0) for d in _month_starts(1990, 1992, 3)]), "Q")
    assert q.frequency == "Q"
    d = parse_bundesbank(
        _bundesbank_csv([(date(1990, 1, 2), 1.7), (date(1990, 1, 3), None)]), DEM_SERIES
    )
    assert d.obs == {date(1990, 1, 2): 1.7}


def test_dem_eur_junction_uses_conversion_rate() -> None:
    dem = Series(DEM_SERIES, {date(1998, 12, 30): 1.6}, "D")
    eur = Series("DEXUSEU", {date(1999, 1, 29): DEM_PER_EUR / 1.6}, "D")
    panel = build_panel({DEM_SERIES: dem, "DEXUSEU": eur}, (1998, 1), (1999, 12))
    assert math.isclose(panel.spot["EUR"][(1998, 12)], panel.spot["EUR"][(1999, 1)])
    jpy = month_end_log_spot(
        Series("J", {date(1990, 1, 5): 100.0, date(1990, 1, 31): 200.0}, "D"), False
    )
    assert math.isclose(jpy[(1990, 1)], math.log(1 / 200.0))


def test_download_cuts_after_2003_and_maps_missing(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> HttpResult:
        calls.append(url)
        sid = url.split("id=")[1].split("&")[0] if "id=" in url else DEM_SERIES
        if sid == "DEXUSUK":
            return HttpResult(200, _fred_csv(sid, [(date(2004, 1, 2), 1.0)]))
        if sid == DEM_SERIES:
            return HttpResult(200, _bundesbank_csv([(date(1990, 1, 2), 1.7)]))
        if sid.endswith("Q156S") or "SLRTTO01" in sid:
            return HttpResult(404, b"")
        return HttpResult(200, _fred_csv(sid, [(date(1990, 1, 1), 1.0)]))

    out = download_sources(
        tmp_path,
        fetcher=fetch,
        sleep=lambda s: None,
        now=lambda: datetime(2026, 11, 26, tzinfo=UTC),
    )
    assert out["DEXUSUK"] == "CORRUPT"
    assert out["LRHUTTTTUSQ156S"] == "NOT_FOUND"
    assert out[DEM_SERIES] == "OK"
    assert all("coed=2003-12-31" in u for u in calls if "fred" in u)
    again = download_sources(tmp_path, fetcher=fetch, sleep=lambda s: None)
    assert again["DEXJPUS"] == "SKIPPED" and again["DEXUSUK"] == "CORRUPT"
    assert len(out) == len(all_fred_series()) + 1


def test_late_observations_on_disk_are_refused(tmp_path: Path) -> None:
    _write_world(tmp_path)
    RatesCache(tmp_path).store("DEXUSAL", b"")  # second file for a series
    with pytest.raises(Exception, match="files"):
        run_l1(
            PREREG,
            data_root=tmp_path,
            research_root=tmp_path / "r",
            code_version="t",
            check_only=True,
        )


def test_check_then_single_run(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_world(data)
    check = run_l1(
        PREREG, data_root=data, research_root=tmp_path / "r", code_version="t", check_only=True
    )
    text = json.dumps(check.report, default=str)
    assert "results" not in check.report
    assert check.report["setup"]["first_holding_month"] is not None
    assert "mean_pct" not in text
    res = run_l1(
        PREREG, data_root=data, research_root=tmp_path / "r", code_version="t", check_only=False
    )
    assert res.report["assessment"]["outcome"] in ("PASSA", "FALLITO")
    assert res.path is not None and res.path.exists()
    assert res.report["results"]["holding_months"][1] == "2003-12"
    assert math.isclose(res.report["results"]["mean_gross_exposure"], 2.0)
    with pytest.raises(ProtocolError, match="one run only"):
        run_l1(
            PREREG, data_root=data, research_root=tmp_path / "r", code_version="t", check_only=False
        )


def test_late_start_stops(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_world(data)
    # Swedish and Norwegian rates only from 1990: at most 7 currencies before that,
    # then remove the Canadian rate as well -> fewer than 7 until 1990.
    for sid in ("IR3TIB01SEM156N", "IR3TIB01NOM156N", "IR3TIB01CAM156N"):
        for f in data.glob(f"{sid}.*.csv"):
            f.unlink()
        rows = [(d, 5.0) for d in _month_starts(1990, 2003)]
        RatesCache(data).store(sid, _fred_csv(sid, rows))
    with pytest.raises(ProtocolError, match="after 1985-12"):
        run_l1(
            PREREG, data_root=data, research_root=tmp_path / "r", code_version="t", check_only=False
        )
    assert set(CURRENCIES) == set(FX_SERIES)
