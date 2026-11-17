"""Read the news archive for the "News e sentiment" page.

Only the archive files written by ``fxnext news-archive`` are read. Every field
is optional: provider formats change, and a missing value shows as "–" instead
of breaking the page. Nothing here feeds the engine or the research (ADR-0008).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Rome")
CURRENCIES = ("USD", "EUR", "GBP", "JPY")
COT_NAMES = {"099741": "EUR", "096742": "GBP", "097741": "JPY"}
DAYS_IT = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2, "Holiday": 3}


def _records_newest_first(
    root: Path, source: str, max_files: int = 400
) -> Iterator[dict[str, Any]]:
    folder = root / source
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*/*.jsonl"), reverse=True)[:max_files]:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def _ok(rec: dict[str, Any]) -> bool:
    return rec.get("http_status") == 200


def _received(rec: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(rec.get("received_at")))
    except ValueError:
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _local(at: datetime | None) -> str:
    return at.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M") if at else "–"


def calendar(root: Path, now: datetime) -> dict[str, Any]:
    """This week's events for the four currencies, grouped by local day."""

    rec = next((r for r in _records_newest_first(root, "calendar") if _ok(r)), None)
    if rec is None or not isinstance(rec.get("body"), list):
        return {"days": [], "received": None, "next_high": None}
    days: dict[str, list[dict[str, Any]]] = {}
    next_high: dict[str, Any] | None = None
    events = []
    for ev in rec["body"]:
        if not isinstance(ev, dict) or ev.get("country") not in CURRENCIES:
            continue
        try:
            at = datetime.fromisoformat(str(ev.get("date"))).astimezone(LOCAL_TZ)
        except ValueError:
            continue
        events.append((at, ev))
    events.sort(key=lambda x: (x[0], IMPACT_ORDER.get(str(x[1].get("impact")), 9)))
    for at, ev in events:
        row = {
            "time": at.strftime("%H:%M"),
            "currency": ev.get("country"),
            "title": ev.get("title") or "–",
            "impact": ev.get("impact") or "",
            "forecast": ev.get("forecast") or "–",
            "previous": ev.get("previous") or "–",
            "actual": ev.get("actual") or "",
            "past": at < now,
        }
        label = f"{DAYS_IT[at.weekday()]} {at:%d/%m}"
        days.setdefault(label, []).append(row)
        if next_high is None and not row["past"] and row["impact"] == "High":
            next_high = {**row, "day": label}
    return {
        "days": list(days.items()),
        "received": _local(_received(rec)),
        "next_high": next_high,
    }


def _net(row: dict[str, Any], long_key: str, short_key: str) -> float | None:
    lo, sh = _float(row.get(long_key)), _float(row.get(short_key))
    return None if lo is None or sh is None else lo - sh


def cot(root: Path) -> dict[str, Any]:
    """Latest COT week per currency with the change from the week before."""

    rec = next((r for r in _records_newest_first(root, "cot") if _ok(r)), None)
    if rec is None or not isinstance(rec.get("body"), list):
        return {"rows": [], "received": None}
    by_code: dict[str, list[dict[str, Any]]] = {}
    for row in rec["body"]:
        if isinstance(row, dict) and row.get("cftc_contract_market_code") in COT_NAMES:
            by_code.setdefault(str(row["cftc_contract_market_code"]), []).append(row)
    out = []
    for code, ccy in COT_NAMES.items():
        weeks = sorted(
            by_code.get(code, []),
            key=lambda r: str(r.get("report_date_as_yyyy_mm_dd")),
            reverse=True,
        )
        if not weeks:
            continue
        last, prev = weeks[0], weeks[1] if len(weeks) > 1 else {}
        oi = _float(last.get("open_interest_all"))
        lev = _net(last, "lev_money_positions_long", "lev_money_positions_short")
        lev_prev = _net(prev, "lev_money_positions_long", "lev_money_positions_short")
        am = _net(last, "asset_mgr_positions_long", "asset_mgr_positions_short")
        am_prev = _net(prev, "asset_mgr_positions_long", "asset_mgr_positions_short")
        out.append(
            {
                "currency": ccy,
                "week": str(last.get("report_date_as_yyyy_mm_dd", ""))[:10],
                "lev": lev,
                "lev_change": None if lev is None or lev_prev is None else lev - lev_prev,
                "lev_pct": None if lev is None or not oi else 100 * lev / oi,
                "am": am,
                "am_change": None if am is None or am_prev is None else am - am_prev,
                "oi": oi,
            }
        )
    return {"rows": out, "received": _local(_received(rec))}


def _symbols(rec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    body = rec.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("symbols"), list):
        return {}
    return {str(s.get("name")): s for s in body["symbols"] if isinstance(s, dict)}


def sentiment(root: Path) -> dict[str, Any]:
    """Latest retail long/short split per pair, and the change over about 24 hours."""

    latest: dict[str, Any] | None = None
    day_before: dict[str, Any] | None = None
    for rec in _records_newest_first(root, "sentiment", max_files=3):
        if not _ok(rec) or not _symbols(rec):
            continue
        if latest is None:
            latest = rec
            continue
        t0, t1 = _received(latest), _received(rec)
        if t0 and t1 and t0 - t1 >= timedelta(hours=23):
            day_before = rec
            break
    if latest is None:
        return {"rows": [], "received": None}
    before = _symbols(day_before) if day_before else {}
    rows = []
    for name, s in sorted(_symbols(latest).items()):
        long_pct = _float(s.get("longPercentage"))
        prev = _float(before.get(name, {}).get("longPercentage"))
        rows.append(
            {
                "symbol": name,
                "long": long_pct,
                "short": _float(s.get("shortPercentage")),
                "change": None if long_pct is None or prev is None else long_pct - prev,
                "positions": _float(s.get("totalPositions")),
            }
        )
    return {"rows": rows, "received": _local(_received(latest))}


def news_page(root: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(tz=UTC)
    return {"calendar": calendar(root, now), "cot": cot(root), "sentiment": sentiment(root)}


__all__ = ["news_page"]
