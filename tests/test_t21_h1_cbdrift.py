"""T21: H1 drift before central bank decisions (h1-cbdrift-001): readers, rules, run."""

from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from fxnext.data.daily import G10_DIGITS, DailyCache, DayCandle, encode_daily
from fxnext.domain.enums import PriceBasis
from fxnext.research.cbdrift import sources
from fxnext.research.cbdrift.events import (
    Event,
    FxPanel,
    Meeting,
    RateGrid,
    _place,
    admission,
    build_events,
    event_t_day,
    expected_change_bp,
    forward_fill,
    leg_return,
    swap_nights,
    weekdays,
)
from fxnext.research.cbdrift.prereg import (
    FROZEN_TEXT_SHA256,
    ONE_MONTH,
    PAIRS,
    RATE_WARMUP_START,
    load_cbdrift,
)
from fxnext.research.cbdrift.runner import month_block_ci, run_cbdrift
from fxnext.research.protocol import ProtocolError

PREREG = Path(__file__).resolve().parents[1] / "prereg" / "h1-cbdrift-001.toml"


# --- pre-registration -------------------------------------------------------------


def test_prereg_points_to_the_frozen_text(tmp_path: Path) -> None:
    pre, _ = load_cbdrift(PREREG)
    assert pre.split == "development" and pre.frozen_text_sha256 == FROZEN_TEXT_SHA256
    text = PREREG.read_text()
    bad = tmp_path / "bad.toml"
    bad.write_text(text.replace(FROZEN_TEXT_SHA256, "0" * 64))
    with pytest.raises(ProtocolError):
        load_cbdrift(bad)
    val = tmp_path / "val.toml"
    val.write_text(text.replace('split = "development"', 'split = "validation"'))
    with pytest.raises(ProtocolError, match="approval"):
        load_cbdrift(val)
    seeds = tmp_path / "seeds.toml"
    seeds.write_text(text.replace("benchmark_seed = 20261124", "benchmark_seed = 1"))
    with pytest.raises(ProtocolError):
        load_cbdrift(seeds)


def test_series_choice_is_fixed_per_run() -> None:
    assert ONE_MONTH["GBP"] == {"development": "IUDVNEA", "validation": "BOE_OIS1M"}
    assert ONE_MONTH["AUD"] == {"development": "FIRMMOIS1D", "validation": "FIRMMBAB30D"}
    assert ONE_MONTH["NOK"] == {"development": None, "validation": None}
    assert all(
        k in sources.SERIES or k == "EUR_ON" for v in ONE_MONTH.values() for k in v.values() if k
    )


# --- readers ----------------------------------------------------------------------


def test_csv_readers() -> None:
    fred = b"observation_date,DTB4WK\n2003-01-02,1.20\n2003-01-03,\n"
    assert sources.parse_column_csv(fred, "observation_date", "DTB4WK") == {date(2003, 1, 2): 1.2}
    boe = b"DATE,IUDSOIA\r\n02 Jan 2003,3.9\r\n"
    assert sources._boe("IUDSOIA")(boe) == {date(2003, 1, 2): 3.9}
    boc = (
        '﻿"TERMS AND CONDITIONS"\r\n"x"\r\n\r\n"SERIES"\r\n"id","label"\r\n"AVG.INTWO","CORRA"'
        '\r\n\r\n"OBSERVATIONS"\r\n"date","AVG.INTWO"\r\n"2003-01-02",2.75\r\n'
    ).encode()
    assert sources._boc("AVG.INTWO")(boc) == {date(2003, 1, 2): 2.75}
    snb = (
        '﻿"CubeId";"zirepo"\r\n"PublishingDate";"2026-11-23 10:00"\r\n\r\n'
        '"Date";"D0";"Value"\r\n"2003-01-02";"H0";0.5\r\n"2003-01-02";"H4";\r\n'
        '"2003-01-03";"H4";0.6\r\n'
    ).encode()
    assert sources.parse_snb(snb, "H0") == {date(2003, 1, 2): 0.5}
    assert sources.parse_snb(snb, "H4") == {date(2003, 1, 3): 0.6}
    norges = b"FREQ;TIME_PERIOD;OBS_VALUE\nB;2003-01-02;5.75\n"
    assert sources._norges(norges) == {date(2003, 1, 2): 5.75}
    ecb = b'KEY,FREQ,TIME_PERIOD,OBS_VALUE,TITLE\r\nX,D,2003-01-02,2.9,"a, b"\r\n'
    assert sources._ecb(ecb) == {date(2003, 1, 2): 2.9}


def test_bde_rba_riksbank_readers() -> None:
    meta = ['"NÚMERO"', '"ALIAS"', '"DESC"', '"UNID"', '"FRECUENCIA"']
    bde = "\n".join(
        ['"CÓDIGO DE LA SERIE",D_DNBAA572,D_DNBAC172']
        + [m + ",x,x" for m in meta]
        + ['"02 ENE 2003","_",2.85', '"03 DIC 2003","...","_"', '"FUENTE","x"', '"NOTAS","y"']
    ).encode("cp1252")
    assert sources.parse_bde(bde, "D_DNBAC172") == {date(2003, 1, 2): 2.85}
    rba = "\n".join(
        ["F1 INTEREST RATES"] + [f"m{i}" for i in range(9)] + ["Series ID,FIRMMCRTD,FIRMMOIS1D"]
        + ["04-Jan-2011,4.75,N/A", "05-Jan-2011,4.75,4.71"]
    ).encode("utf-8-sig")  # fmt: skip
    assert sources.parse_rba_csv(rba, "FIRMMOIS1D") == {date(2011, 1, 5): 4.71}
    rik = json.dumps([{"date": "2003-01-02", "value": 3}, {"date": "2003-01-03", "value": 3.25}])
    assert sources.parse_riksbank(rik.encode()) == {date(2003, 1, 2): 3.0, date(2003, 1, 3): 3.25}


def _xlsx(rows: list[list[object]], sheet: str) -> bytes:
    import openpyxl  # type: ignore[import-untyped]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_readers() -> None:
    from datetime import datetime

    rbnz = _xlsx(
        [["g"], ["n"], ["Notes"], ["Unit"], ["Series Id", "INM.DP1.N", "INM.DB01.NZZV"],
         [datetime(2009, 1, 28), 5.0, None], [datetime(2009, 1, 29), 3.5, 3.4]],
        "Data",
    )  # fmt: skip
    assert sources.parse_rbnz_xlsx(rbnz, "INM.DB01.NZZV") == {date(2009, 1, 29): 3.4}
    head = [[None, "UK OIS spot curve"], ["Maturity "], ["months:", 1, 2], ["years:", 1 / 12], []]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, (inner, sheet) in enumerate(sources.BOE_OIS_SHEETS.items()):
            z.writestr(inner, _xlsx(head + [[datetime(2009 + i * 7, 1, 2), 1.0 + i, 9.0]], sheet))
    out = sources.parse_boe_ois(buf.getvalue())
    assert out == {date(2009, 1, 2): 1.0, date(2016, 1, 2): 2.0, date(2023, 1, 2): 3.0}


def test_eur_overnight_has_no_jump() -> None:
    eonia = {date(2021, 12, 31): -0.49, date(2022, 1, 3): -0.49}
    estr = {date(2021, 12, 31): -0.575, date(2022, 1, 3): -0.575}
    out = sources.eur_overnight(eonia, estr)
    assert out[date(2021, 12, 31)] == -0.49 and math.isclose(out[date(2022, 1, 3)], -0.49)


# --- rules ------------------------------------------------------------------------


def test_forward_fill_stops_after_five_weekdays() -> None:
    x = np.array([1.0] + [np.nan] * 7)
    out = forward_fill(x, 5)
    assert np.isnan(out[6]) and out[5] == 1.0


def test_expected_change_corrects_the_premium_and_scales() -> None:
    n = 300
    one = np.full(n, 2.5)  # 1-month 50 bp above overnight: structural premium
    on = np.full(n, 2.0)
    delta, why = expected_change_bp(one, on, one, on, n - 1, 16)
    assert why is None and delta == 0.0
    one2 = one.copy()
    one2[-5:] = 2.6  # +10 bp in the last five days
    delta, _ = expected_change_bp(one2, on, one2, on, n - 1, 16)
    assert delta == pytest.approx(10.0 * 30.0 / 14.0)
    _, why = expected_change_bp(one, on, one, on, 100, 16)
    assert why == "rates_warmup"


def test_timing_and_rbnz_day() -> None:
    days = weekdays(date(2005, 1, 3), date(2005, 3, 1))
    fx = FxPanel(days, {"EUR": np.ones(len(days))}, {"EUR": np.ones(len(days))})
    ev = Event("ECB", "EUR", date(2005, 2, 3), date(2005, 2, 3), "hold")
    _place(ev, fx.valid("EUR"), fx, date(2004, 1, 1), date(2014, 1, 1))
    assert ev.exit_day == date(2005, 2, 2) and ev.entry_day == date(2005, 1, 19)
    assert ev.signal_day == date(2005, 1, 18)
    assert event_t_day("RBNZ", date(2005, 2, 3)) == date(2005, 2, 2)
    late = Event("ECB", "EUR", date(2005, 3, 3), date(2005, 3, 3), "hold")
    _place(late, fx.valid("EUR"), fx, date(2004, 1, 1), date(2005, 3, 1))
    assert late.excluded == "exit_after_split"


def test_swap_nights_triple_wednesday() -> None:
    assert swap_nights(date(2005, 1, 3), date(2005, 1, 10)) == 7  # Mon -> next Mon
    assert swap_nights(date(2005, 1, 6), date(2005, 1, 7)) == 1  # Thu -> Fri


def test_leg_return_signs_and_costs() -> None:
    days = [date(2005, 1, 3), date(2005, 1, 17)]
    grid = RateGrid.build(
        {
            k: dict.fromkeys(days, v)
            for k, v in (("DTB4WK", 2.0), ("SAR1M", 0.5), ("EURIBOR1M", 2.0))
        },
        days,
    )
    fx = FxPanel(
        days,
        {"CHF": np.array([1.2000, 1.1880]), "EUR": np.array([1.3, 1.3])},
        {"CHF": np.array([1.2002, 1.1882]), "EUR": np.array([1.3, 1.3])},
    )
    fxr, carry, spread, slip, markup, filled = leg_return(
        "CHF", fx, 0, 1, grid, 0, "development", days[0], days[1], {"CHF": 1e-4}
    )
    assert fxr == pytest.approx(1.2001 / 1.1881 - 1.0)  # USDCHF fell: CHF rose
    assert carry == pytest.approx(-1.5 / 100 * 14 / 360)
    assert spread == pytest.approx(0.5 * 0.0002 / 1.2001 + 0.5 * 0.0002 / 1.1881)
    assert markup == pytest.approx(0.016 * 14 / 365) and filled == 0 and slip > 0
    *_, filled = leg_return(
        "EUR", fx, 0, 1, grid, 0, "development", days[0], days[1], {"EUR": 2e-4}
    )
    assert filled == 2


def test_month_block_ci_is_seeded() -> None:
    x = np.arange(40, dtype=float)
    months = [(2005, 1 + i // 4) for i in range(40)]
    a = month_block_ci(x, months, samples=500, seed=1)
    assert a == month_block_ci(x, months, samples=500, seed=1)
    assert a is not None and a[0] < x.mean() < a[1]


# --- admission and end to end -------------------------------------------------------

BASE = {
    "DTB4WK": 2.0, "DFF": 2.0, "EURIBOR1M": 2.2, "EONIA": 2.0, "ESTR": 1.9, "IUDVNEA": 4.2,
    "IUDSOIA": 4.0, "BOE_OIS1M": 4.0, "FIRMMOIS1D": 5.0, "FIRMMBAB30D": 5.1, "FIRMMCRTD": 5.0,
    "TB.CDN.30D.MID": 2.9, "AVG.INTWO": 3.0, "SETB1MBENCHC": 2.4, "SECBREPOEFF": 2.5,
    "INM.DB01.NZZV": 6.1, "INM.DP1.N": 6.0, "SAR1M": 0.6, "SARON": 0.5, "IR/B.KPRA.SD.R": 4.0,
}  # fmt: skip
START = {"EUR": 1.2, "GBP": 1.8, "AUD": 0.75, "NZD": 0.65, "CHF": 1.3, "CAD": 1.3, "SEK": 7.5}


def _series(key: str, last: date) -> dict[date, float]:
    first = date(2009, 1, 29) if key == "INM.DB01.NZZV" else RATE_WARMUP_START
    out = {}
    for d in weekdays(first, last):
        v = BASE[key]
        # The ECB "prices" a hike before each March meeting: +30 bp in early March.
        if key == "EURIBOR1M" and d.month == 2 and d.day >= 10:
            v += 0.3
        out[d] = v
    return out


def _write_rates(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root.mkdir(parents=True)
    last = date(2014, 6, 1)
    data = {k: _series(k, last) for k in BASE}
    # One file per series in a simple narrow CSV, read through stand-in readers: the
    # real layouts are covered by the reader tests above.
    stand_in: dict[str, list[tuple[str, object]]] = {}
    for key, parts in sources.SERIES.items():
        name = f"{key.replace('/', '_')}.csv"
        lines = ["date,value"] + [f"{d.isoformat()},{v}" for d, v in data[key].items()]
        (root / name).write_text("\n".join(lines) + "\n")
        stand_in[key] = [(name, lambda p: sources.parse_column_csv(p, "date", "value"))]
        assert parts
    monkeypatch.setattr(sources, "SERIES", stand_in)


def _write_fx(root: Path) -> None:
    cache = DailyCache(root)
    for n, (ccy, symbol) in enumerate(PAIRS.items()):
        if ccy == "NOK":
            continue
        for basis in ("BID", "ASK"):
            rng = np.random.Generator(np.random.PCG64(50 + n))
            price = START[ccy]
            for year in range(2003, 2014):
                candles = []
                d = date(year, 1, 1)
                while d.year == year:
                    if d.weekday() >= 5:
                        candles.append(DayCandle(d, price, price, price, price, 0.0))
                    else:
                        price *= float(np.exp(rng.normal(0.0, 0.005)))
                        p = price * (1 + (1e-4 if basis == "ASK" else -1e-4))
                        candles.append(DayCandle(d, p, p, p, p, 100.0))
                    d += timedelta(days=1)
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


def _write_calendar(path: Path) -> None:
    rows = ["bank,announcement_date_local,scheduled,decision"]
    for y in range(2004, 2014):
        rows.append(f"ECB,{y}-03-03,yes,hold")
        rows.append(f"Fed,{y}-06-20,yes,hold")
        rows.append(f"RBNZ,{y}-04-24,yes,hold")
        rows.append(f"RBA,{y}-05-06,yes,hold")
    rows.append("BoE,2008-10-08,no,cut")
    rows.append("ECB,2014-01-09,yes,hold")  # after the split: never an event
    path.write_text("\n".join(rows) + "\n")


@pytest.fixture()
def h1_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {
        "rates": tmp_path / "rates",
        "cache": tmp_path / "raw-d1",
        "calendar": tmp_path / "meetings.csv",
        "research": tmp_path / "research",
    }
    _write_rates(paths["rates"], monkeypatch)
    _write_fx(paths["cache"])
    _write_calendar(paths["calendar"])
    return paths


def test_admission_drops_nok_and_nzd_in_development(h1_data: dict[str, Path]) -> None:
    series, _, _ = sources.load_rates(h1_data["rates"], date(2014, 1, 1))
    assert max(series["DTB4WK"]) < date(2014, 1, 1)  # nothing from the validation years
    grid = RateGrid.build(series, weekdays(RATE_WARMUP_START, date(2014, 1, 1)))
    admitted, info = admission(grid, "development")
    assert "NOK" not in admitted and "NZD" not in admitted
    assert set(admitted) == {"EUR", "GBP", "CHF", "AUD", "CAD", "SEK"}
    assert info["NZD"]["coverage"]["one_month"] < 0.9


def test_end_to_end_development(h1_data: dict[str, Path]) -> None:
    kw = {
        "cache_root": h1_data["cache"],
        "rates_root": h1_data["rates"],
        "calendar_path": h1_data["calendar"],
        "research_root": h1_data["research"],
        "code_version": "test",
    }
    check = run_cbdrift(PREREG, check_only=True, **kw)
    assert "net" not in check.report and check.report["data_check"]["admitted"]
    res = run_cbdrift(PREREG, **kw)
    r = res.report
    assert r["assessment"]["outcome"] in ("PROPOSE_VALIDATION", "CLOSE_ON_DEV")
    # The ECB hike priced every February is the only signal: 10 hikes, all EUR.
    assert r["counts"]["hikes_expected"] == 10 and r["counts"]["cuts_expected"] == 0
    assert r["by_bank"]["ECB"]["events"] == 10 and r["by_bank"]["Fed"]["events"] == 0
    ex = r["counts"]["excluded"]
    assert ex["currency_not_admitted"] == 10  # RBNZ in development
    assert ex["rba_before_2007_12"] == 4 and ex["unscheduled"] == 1
    assert (res.path / "report.md").exists() and (res.path / "events.csv").exists()
    again = run_cbdrift(PREREG, **kw)
    assert again.fingerprint == res.fingerprint


def test_fed_trades_the_basket() -> None:
    days = weekdays(date(2004, 1, 1), date(2006, 1, 1))
    series = {k: dict.fromkeys(days, 2.0) for k in ("DTB4WK", "DFF", "EURIBOR1M", "EUR_ON")}
    for d in days[-40:]:
        series["DTB4WK"][d] = 2.5  # a Fed hike priced at the end
    grid = RateGrid.build(series, days)
    fx = FxPanel(days, {"EUR": np.ones(len(days))}, {"EUR": np.ones(len(days)) * 1.0001})
    meetings = [Meeting("Fed", days[-3], True, "hike")]
    events, _ = build_events(meetings, "development", fx, grid, ["EUR"])
    (ev,) = events
    assert ev.direction == 1 and ev.position == -1 and ev.legs == ["EUR"]
