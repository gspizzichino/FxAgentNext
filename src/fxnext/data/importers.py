"""Importers: Dukascopy raw cache (M1 -> M15) and generic CSV (contracts §3, §13)."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from fxnext.data import bi5
from fxnext.data.dukascopy import FetchStatus, RawCache, iter_days
from fxnext.data.rows import ImportResult, Rejected, RowDraft, check_ohlc, is_flat_zero
from fxnext.domain.calendar import FxCalendar
from fxnext.domain.enums import Availability, PriceBasis, RejectReason, SlotStatus, Timeframe
from fxnext.domain.timeutil import ensure_utc, to_ns

_M15 = Timeframe.M15.ns
_MIN = 60_000_000_000


def _session_set(calendar: FxCalendar, start: datetime, end: datetime) -> set[int]:
    return {int(x) for x in calendar.slot_grid(Timeframe.M15, start, end)}


def import_dukascopy(
    cache: RawCache,
    symbol: str,
    basis: PriceBasis,
    start: date,
    end: date,
    calendar: FxCalendar | None = None,
) -> tuple[ImportResult, list[str]]:
    """Build M15 rows for [start, end] (inclusive UTC days). Returns rows and raw sha256s."""

    calendar = calendar or FxCalendar()
    digits = bi5.PRICE_DIGITS[symbol]
    scale = 10**digits
    t0 = datetime(start.year, start.month, start.day, tzinfo=UTC)
    t1 = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    in_session = _session_set(calendar, t0, t1)
    resolved = cache.resolved()
    result = ImportResult()
    raw_hashes: list[str] = []
    buckets: dict[int, list[bi5.MinuteCandle]] = defaultdict(list)
    for day in iter_days(start, end):
        rec = resolved.get((symbol, basis.value, day.isoformat()))
        if rec is None:
            result.quality["days_not_downloaded"] += 1
            continue
        if rec["status"] != FetchStatus.OK:
            result.quality[f"days_{rec['status'].lower()}"] += 1
            continue
        payload = (cache.root / rec["cache_path"]).read_bytes()
        raw_hashes.append(rec["sha256"])
        day_ns = to_ns(datetime(day.year, day.month, day.day, tzinfo=UTC))
        for candle in bi5.decode(payload):
            result.quality["minutes_read"] += 1
            if candle.second_of_day % 60 or not 0 <= candle.second_of_day < 86_400:
                result.quality["minutes_off_grid"] += 1
                result.rejected.append(
                    Rejected(
                        day_ns + (candle.second_of_day // 900) * _M15,
                        day_ns,
                        RejectReason.OFF_GRID,
                        f"second_of_day={candle.second_of_day}",
                    )
                )
                continue
            minute_ns = day_ns + candle.second_of_day * 1_000_000_000
            buckets[minute_ns - minute_ns % _M15].append(candle)
    for slot_ns in sorted(buckets):
        candles = buckets[slot_ns]
        close_ns = slot_ns + _M15
        flat = all(is_flat_zero(c.open, c.high, c.low, c.close, c.volume) for c in candles)
        if slot_ns not in in_session:
            result.quality["closed_filler" if flat else "out_of_session"] += 1
            continue
        by_minute: dict[int, bi5.MinuteCandle] = {}
        conflict = False
        for c in candles:
            prev = by_minute.get(c.second_of_day)
            if prev is None:
                by_minute[c.second_of_day] = c
            elif prev == c:
                result.quality["duplicates_identical"] += 1
            else:
                conflict = True
        if conflict:
            result.rejected.append(
                Rejected(
                    slot_ns,
                    close_ns,
                    RejectReason.CONFLICTING_DUPLICATE,
                    "conflicting minute records",
                )
            )
            continue
        if len(by_minute) != 15:
            result.rejected.append(
                Rejected(
                    slot_ns,
                    close_ns,
                    RejectReason.INCOMPLETE_SOURCE,
                    f"{len(by_minute)}/15 minutes",
                )
            )
            continue
        ordered = [by_minute[k] for k in sorted(by_minute)]
        bad = next((r for c in ordered if (r := check_ohlc(c.open, c.high, c.low, c.close))), None)
        if bad is not None:
            result.rejected.append(Rejected(slot_ns, close_ns, bad, "minute record"))
            continue
        volume = 0.0
        for c in ordered:
            volume += float(c.volume)  # sequential sum, fixed order
        o, c_ = ordered[0].open, ordered[-1].close
        h, lo = max(c.high for c in ordered), min(c.low for c in ordered)
        status = SlotStatus.FLAT_ZERO_VOLUME if flat else SlotStatus.VALID
        result.rows.append(
            RowDraft(
                slot_ns,
                close_ns,
                Availability.MODELLED,
                status,
                o / scale,
                h / scale,
                lo / scale,
                c_ / scale,
                volume,
            )
        )
    _count_missing(result, in_session)
    return result, raw_hashes


def _count_missing(result: ImportResult, in_session: set[int]) -> None:
    present = {r.open_time_ns for r in result.rows} | {r.open_time_ns for r in result.rejected}
    result.quality["in_session_slots"] = len(in_session)
    result.quality["in_session_missing"] = len(in_session - present)
    result.quality["rows_valid"] = sum(r.status is SlotStatus.VALID for r in result.rows)
    result.quality["rows_flat_zero_volume"] = sum(
        r.status is SlotStatus.FLAT_ZERO_VOLUME for r in result.rows
    )
    result.quality["rows_rejected"] = len(result.rejected)


def cross_check_bid_ask(bid: ImportResult, ask: ImportResult) -> None:
    """T8.9: ask_open >= bid_open and ask_close >= bid_close, else both slots INVALID."""

    ask_by = {r.open_time_ns: r for r in ask.rows}
    bad: set[int] = set()
    for r in bid.rows:
        a = ask_by.get(r.open_time_ns)
        if a is not None and (a.open < r.open or a.close < r.close):
            bad.add(r.open_time_ns)
    for res in (bid, ask):
        keep = []
        for r in res.rows:
            if r.open_time_ns in bad:
                res.rejected.append(
                    Rejected(
                        r.open_time_ns, r.available_at_ns, RejectReason.NEGATIVE_SPREAD, "ask < bid"
                    )
                )
            else:
                keep.append(r)
        res.rows[:] = keep
        res.quality["rows_negative_spread"] = len(bad)
        res.quality["rows_valid"] = sum(r.status is SlotStatus.VALID for r in res.rows)
        res.quality["rows_flat_zero_volume"] = sum(
            r.status is SlotStatus.FLAT_ZERO_VOLUME for r in res.rows
        )
        res.quality["rows_rejected"] = len(res.rejected)


def import_generic_csv(
    path: Path, start: datetime, end: datetime, calendar: FxCalendar | None = None
) -> ImportResult:
    """Generic M15 CSV: open_time,open,high,low,close,volume[,available_at] (ISO-8601 UTC)."""

    calendar = calendar or FxCalendar()
    in_session = _session_set(calendar, start, end)
    result = ImportResult()
    seen: dict[int, tuple[float, ...]] = {}
    rows: dict[int, RowDraft] = {}
    conflicts: set[int] = set()
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            result.quality["rows_read"] += 1
            open_ns = to_ns(ensure_utc(datetime.fromisoformat(raw["open_time"])))
            close_ns = open_ns + _M15
            values = tuple(float(raw[k]) for k in ("open", "high", "low", "close", "volume"))
            avail_raw = raw.get("available_at") or ""
            if avail_raw:
                avail_ns = to_ns(ensure_utc(datetime.fromisoformat(avail_raw)))
                availability = Availability.OBSERVED
            else:
                avail_ns, availability = close_ns, Availability.MODELLED
            if open_ns % _M15:
                result.rejected.append(
                    Rejected(
                        open_ns - open_ns % _M15, avail_ns, RejectReason.OFF_GRID, raw["open_time"]
                    )
                )
                continue
            if open_ns not in in_session:
                flat = is_flat_zero(*values)
                result.quality["closed_filler" if flat else "out_of_session"] += 1
                continue
            if avail_ns < close_ns:
                result.rejected.append(
                    Rejected(
                        open_ns, close_ns, RejectReason.OFF_GRID, "available_at before close_time"
                    )
                )
                continue
            if open_ns in seen:
                if seen[open_ns] == values + (avail_ns,):
                    result.quality["duplicates_identical"] += 1
                else:
                    conflicts.add(open_ns)
                continue
            seen[open_ns] = values + (avail_ns,)
            o, h, lo, c, vol = values
            bad = check_ohlc(o, h, lo, c)
            if bad is not None or not np.isfinite(vol) or vol < 0:
                result.rejected.append(
                    Rejected(open_ns, avail_ns, bad or RejectReason.NON_FINITE, "row")
                )
                continue
            status = (
                SlotStatus.FLAT_ZERO_VOLUME if is_flat_zero(o, h, lo, c, vol) else SlotStatus.VALID
            )
            rows[open_ns] = RowDraft(open_ns, avail_ns, availability, status, o, h, lo, c, vol)
    for open_ns in sorted(conflicts):
        rows.pop(open_ns, None)
        result.rejected = [r for r in result.rejected if r.open_time_ns != open_ns]
        result.rejected.append(
            Rejected(
                open_ns, open_ns + _M15, RejectReason.CONFLICTING_DUPLICATE, "conflicting rows"
            )
        )
    result.rows = list(rows.values())
    _count_missing(result, in_session)
    return result
