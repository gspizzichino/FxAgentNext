"""News page: reads the archive, tolerates missing fields, shows local times."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from fxnext.dashboard import readers as rd
from fxnext.dashboard.app import create_app
from fxnext.dashboard.news_view import news_page
from fxnext.news import archive as na

CAL = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-11-18T08:30:00-05:00",
     "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
    {"title": "Retail Sales", "country": "GBP", "date": "2026-11-16T02:00:00-05:00",
     "impact": "Medium", "forecast": "", "previous": "0.1%"},
    {"title": "Some AUD thing", "country": "AUD", "date": "2026-11-17T19:30:00-05:00",
     "impact": "High"},
    {"title": "broken", "country": "EUR", "date": "not a date"},
]  # fmt: skip
COT = [
    {"cftc_contract_market_code": "099741", "report_date_as_yyyy_mm_dd": "2026-11-10T00:00:00.000",
     "lev_money_positions_long": "100", "lev_money_positions_short": "40",
     "asset_mgr_positions_long": "500", "asset_mgr_positions_short": "100",
     "open_interest_all": "1000"},
    {"cftc_contract_market_code": "099741", "report_date_as_yyyy_mm_dd": "2026-11-03T00:00:00.000",
     "lev_money_positions_long": "90", "lev_money_positions_short": "50",
     "asset_mgr_positions_long": "450", "asset_mgr_positions_short": "100"},
    {"cftc_contract_market_code": "097741", "report_date_as_yyyy_mm_dd": "2026-11-10T00:00:00.000"},
]  # fmt: skip


def _senti(long_pct: int) -> bytes:
    sym = [{"name": "EURUSD", "longPercentage": long_pct, "shortPercentage": 100 - long_pct}]
    return json.dumps({"error": False, "symbols": sym}).encode()


def _archive(root: Path) -> None:
    t = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    na.store(root, na.Snapshot("calendar", "u", 200, json.dumps(CAL).encode()), t)
    na.store(root, na.Snapshot("cot", "u", 200, json.dumps(COT).encode()), t)
    na.store(root, na.Snapshot("sentiment", "u", 200, _senti(40)), t)
    t2 = datetime(2026, 11, 17, 12, 30, tzinfo=UTC)
    na.store(root, na.Snapshot("sentiment", "u", 200, _senti(45)), t2)
    na.store(root, na.Snapshot("sentiment", "u", 503, b"busy"), t2)  # errors are skipped


def test_news_page_values(tmp_path: Path) -> None:
    _archive(tmp_path)
    page = news_page(tmp_path, now=datetime(2026, 11, 17, 0, 0, tzinfo=UTC))
    days = dict(page["calendar"]["days"])
    assert list(days) == ["lun 16/11", "mer 18/11"]  # AUD and the broken date are left out
    assert days["mer 18/11"][0]["time"] == "14:30"  # 08:30 New York = 14:30 Rome
    assert days["lun 16/11"][0]["past"] and days["lun 16/11"][0]["forecast"] == "–"
    assert page["calendar"]["next_high"]["title"] == "CPI m/m"
    eur, jpy = page["cot"]["rows"]
    assert (eur["lev"], eur["lev_change"], eur["lev_pct"]) == (60, 20, 6.0)
    assert (eur["am"], eur["am_change"]) == (400, 50)
    assert jpy["lev"] is None and jpy["lev_change"] is None
    (s,) = page["sentiment"]["rows"]
    assert (s["long"], s["change"]) == (45, 5)


def test_news_page_renders_empty_and_full(tmp_path: Path) -> None:
    empty = TestClient(create_app(rd.Sources(news=tmp_path / "none"))).get("/news")
    assert empty.status_code == 200 and "Ancora nessun calendario" in empty.text
    _archive(tmp_path)
    page = TestClient(create_app(rd.Sources(news=tmp_path))).get("/news")
    assert page.status_code == 200
    assert "CPI m/m" in page.text and "45% long" in page.text and 'data-refresh="30"' in page.text
