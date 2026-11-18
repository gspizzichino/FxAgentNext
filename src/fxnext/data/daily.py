"""Dukascopy daily candles (one file per pair, basis and year) and FRED rate files.

Same rules as the M1 cache (ADR-0015): only the ``download_*`` functions touch the
network, every finished attempt is logged, cached files are content addressed and
never overwritten.

Daily file: ``{SYM}/{YYYY}/{BASIS}_candles_day_1.bi5``, LZMA, 24-byte records like
the minute files but with the time in seconds from 00:00 UTC of 1 January. One
candle per UTC calendar day (weekends included, flat with zero volume when closed).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import lzma
import os
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fxnext.data.dukascopy import (
    DEFAULT_BASE_URL,
    Fetcher,
    FetchStatus,
    HttpResult,
    TransientHttpError,
    urllib_fetcher,
)
from fxnext.domain.enums import PriceBasis

DAILY_DOWNLOADER_VERSION = "dukascopy-daily-downloader-v1"
DAILY_PARSER_VERSION = "dukascopy-bi5-day-candles-v1"
RATES_DOWNLOADER_VERSION = "fred-csv-downloader-v2"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

_RECORD = struct.Struct(">Iiiiif")

# Integer price units of the Dukascopy files (10**-digits).
G10_DIGITS = {
    "EURUSD": 5,
    "GBPUSD": 5,
    "AUDUSD": 5,
    "NZDUSD": 5,
    "USDJPY": 3,
    "USDCHF": 5,
    "USDCAD": 5,
    "USDNOK": 5,
    "USDSEK": 5,
}

# OECD 3-month interbank rates, monthly, percent per year (FRED ids).
FRED_SERIES = {
    "USD": "IR3TIB01USM156N",
    "EUR": "IR3TIB01EZM156N",
    "GBP": "IR3TIB01GBM156N",
    "JPY": "IR3TIB01JPM156N",
    "CHF": "IR3TIB01CHM156N",
    "AUD": "IR3TIB01AUM156N",
    "NZD": "IR3TIB01NZM156N",
    "CAD": "IR3TIB01CAM156N",
    "NOK": "IR3TIB01NOM156N",
    "SEK": "IR3TIB01SEM156N",
}


class DailyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DayCandle:
    day: date
    open: float
    close: float
    low: float
    high: float
    volume: float


def daily_url(base_url: str, symbol: str, basis: PriceBasis, year: int) -> str:
    return f"{base_url.rstrip('/')}/{symbol}/{year:04d}/{basis.value}_candles_day_1.bi5"


def decode_daily(payload: bytes, year: int, digits: int) -> tuple[DayCandle, ...]:
    if not payload:
        return ()
    try:
        raw = lzma.decompress(payload, format=lzma.FORMAT_AUTO)
    except lzma.LZMAError as error:
        raise DailyError(f"LZMA decode failed: {error}") from error
    if len(raw) % _RECORD.size:
        raise DailyError(f"truncated payload: {len(raw)} bytes")
    origin = datetime(year, 1, 1, tzinfo=UTC)
    scale = 10.0**digits
    out = []
    for offset in range(0, len(raw), _RECORD.size):
        sec, o, c, lo, h, vol = _RECORD.unpack_from(raw, offset)
        if sec % 86_400:
            raise DailyError(f"candle not on a UTC day boundary: {sec}")
        day = (origin + timedelta(seconds=sec)).date()
        if day.year != year:
            raise DailyError(f"candle outside {year}: {day}")
        out.append(DayCandle(day, o / scale, c / scale, lo / scale, h / scale, float(vol)))
    return tuple(out)


def encode_daily(candles: list[DayCandle], digits: int) -> bytes:
    """Inverse of ``decode_daily``; used by tests to build fixture files."""

    scale = 10**digits
    raw = b"".join(
        _RECORD.pack(
            (c.day - date(c.day.year, 1, 1)).days * 86_400,
            round(c.open * scale),
            round(c.close * scale),
            round(c.low * scale),
            round(c.high * scale),
            c.volume,
        )
        for c in candles
    )
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


class _HashedCache:
    """Content addressed files plus an append-only ``fetch_log.jsonl``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.log_path = self.root / "fetch_log.jsonl"

    def records(self) -> Iterator[dict[str, Any]]:
        if not self.log_path.exists():
            return
        with self.log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def append(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _store(self, directory: Path, stem: str, suffix: str, payload: bytes) -> Path:
        digest = hashlib.sha256(payload).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{stem}.{digest[:12]}{suffix}"
        if final.exists():
            return final
        tmp = directory / f".tmp-{stem}-{os.getpid()}"
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
        return final


class DailyCache(_HashedCache):
    """``root/{SYMBOL}/{BASIS}/{YYYY}.{sha12}.bi5`` + ``fetch_log.jsonl``."""

    def resolved(self) -> dict[tuple[str, str, int], dict[str, Any]]:
        """Latest terminal record (OK/EMPTY/NOT_FOUND) per (symbol, basis, year)."""

        out: dict[tuple[str, str, int], dict[str, Any]] = {}
        for rec in self.records():
            if rec["status"] in (FetchStatus.OK, FetchStatus.EMPTY, FetchStatus.NOT_FOUND):
                out[(rec["symbol"], rec["basis"], int(rec["year"]))] = rec
        return out

    def store(self, symbol: str, basis: PriceBasis, year: int, payload: bytes) -> Path:
        return self._store(self.root / symbol / basis.value, f"{year:04d}", ".bi5", payload)


class RatesCache(_HashedCache):
    """``root/{SERIES}.{sha12}.csv`` + ``fetch_log.jsonl``."""

    def resolved(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for rec in self.records():
            if rec["status"] == FetchStatus.OK:
                out[rec["series"]] = rec
        return out

    def store(self, series: str, payload: bytes) -> Path:
        return self._store(self.root, series, ".csv", payload)


def _fetch(
    url: str,
    fetcher: Fetcher,
    max_attempts: int,
    backoff: float,
    sleep: Callable[[float], None],
    timeout: float,
) -> tuple[HttpResult | None, int, str]:
    attempts, error = 0, ""
    while attempts < max_attempts:
        attempts += 1
        try:
            return fetcher(url, timeout), attempts, ""
        except TransientHttpError as exc:
            error = str(exc)
            if attempts < max_attempts:
                sleep(backoff * (2 ** (attempts - 1)))
    return None, attempts, error


def download_daily(
    cache: DailyCache,
    symbols: list[str],
    bases: list[PriceBasis],
    first_year: int,
    last_year: int,
    *,
    base_url: str = DEFAULT_BASE_URL,
    fetcher: Fetcher = urllib_fetcher,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_attempts: int = 4,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 30.0,
    min_interval: float = 1.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Download whole elapsed years; a year already resolved is skipped."""

    counts = dict.fromkeys(("requested", "skipped", "ok", "not_found", "failed", "corrupt"), 0)
    resolved = cache.resolved()
    current = now()
    first = True
    for symbol in symbols:
        digits = G10_DIGITS[symbol]
        for basis in bases:
            for year in range(first_year, last_year + 1):
                if datetime(year + 1, 1, 1, 2, tzinfo=UTC) > current:
                    continue  # a year file is final only after the year has ended
                if (symbol, basis.value, year) in resolved:
                    counts["skipped"] += 1
                    continue
                counts["requested"] += 1
                if not first and min_interval > 0:
                    sleep(min_interval)
                first = False
                url = daily_url(base_url, symbol, basis, year)
                result, attempts, error = _fetch(
                    url, fetcher, max_attempts, backoff_seconds, sleep, timeout
                )
                record: dict[str, Any] = {
                    "url": url,
                    "symbol": symbol,
                    "basis": basis.value,
                    "year": year,
                    "http_status": None if result is None else result.status,
                    "attempts": attempts,
                    "bytes": 0,
                    "sha256": None,
                    "cache_path": None,
                    "fetched_at": now().isoformat(),
                    "downloader_version": DAILY_DOWNLOADER_VERSION,
                    "parser_version": DAILY_PARSER_VERSION,
                    "detail": error,
                }
                if result is None:
                    record["status"] = FetchStatus.FAILED
                elif result.status == 404:
                    record["status"] = FetchStatus.NOT_FOUND
                elif result.status != 200 or not result.body:
                    record["status"] = FetchStatus.FAILED
                    record["detail"] = f"HTTP {result.status}, {len(result.body)} bytes"
                else:
                    try:
                        decode_daily(result.body, year, digits)
                    except DailyError as exc:
                        record.update(status=FetchStatus.CORRUPT, detail=str(exc))
                    else:
                        path = cache.store(symbol, basis, year, result.body)
                        record.update(
                            status=FetchStatus.OK,
                            bytes=len(result.body),
                            sha256=hashlib.sha256(result.body).hexdigest(),
                            cache_path=path.relative_to(cache.root).as_posix(),
                        )
                cache.append(record)
                if progress is not None:
                    progress(record)
                key = {
                    FetchStatus.OK: "ok",
                    FetchStatus.NOT_FOUND: "not_found",
                    FetchStatus.FAILED: "failed",
                    FetchStatus.CORRUPT: "corrupt",
                }[record["status"]]
                counts[key] += 1
    return counts


def fred_fetcher(url: str, timeout: float) -> HttpResult:
    """Plain urllib request: FRED does not answer the project's custom User-Agent
    (requests time out), while it answers the standard Python one at once."""

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return HttpResult(int(response.status), response.read())
    except urllib.error.HTTPError as error:
        if error.code >= 500 or error.code == 429:
            raise TransientHttpError(f"HTTP {error.code}") from error
        return HttpResult(int(error.code), b"")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise TransientHttpError(str(error)) from error


def download_rates(
    cache: RatesCache,
    series: list[str],
    *,
    fetcher: Fetcher = fred_fetcher,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_attempts: int = 4,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 30.0,
) -> dict[str, str]:
    """Fetch each FRED series once (skip if already cached). Returns series -> status."""

    resolved = cache.resolved()
    out: dict[str, str] = {}
    for sid in series:
        if sid in resolved:
            out[sid] = "SKIPPED"
            continue
        url = FRED_URL.format(series=sid)
        result, attempts, error = _fetch(
            url, fetcher, max_attempts, backoff_seconds, sleep, timeout
        )
        record: dict[str, Any] = {
            "url": url,
            "series": sid,
            "http_status": None if result is None else result.status,
            "attempts": attempts,
            "sha256": None,
            "cache_path": None,
            "fetched_at": now().isoformat(),
            "downloader_version": RATES_DOWNLOADER_VERSION,
            "detail": error,
        }
        if result is None or result.status != 200 or not result.body:
            record["status"] = FetchStatus.FAILED
        else:
            try:
                parse_fred_csv(result.body, sid)
            except DailyError as exc:
                record.update(status=FetchStatus.CORRUPT, detail=str(exc))
            else:
                path = cache.store(sid, result.body)
                record.update(
                    status=FetchStatus.OK,
                    sha256=hashlib.sha256(result.body).hexdigest(),
                    cache_path=path.relative_to(cache.root).as_posix(),
                )
        cache.append(record)
        out[sid] = record["status"]
    return out


def parse_fred_csv(payload: bytes, series: str) -> dict[tuple[int, int], float | None]:
    """Monthly FRED CSV -> {(year, month): percent or None when missing}."""

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DailyError(f"{series}: not UTF-8") from error
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None or len(header) != 2 or header[1].strip() != series:
        raise DailyError(f"{series}: unexpected header {header}")
    out: dict[tuple[int, int], float | None] = {}
    for row in reader:
        if not row:
            continue
        if len(row) != 2:
            raise DailyError(f"{series}: bad row {row}")
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError as error:
            raise DailyError(f"{series}: bad date {row[0]}") from error
        if day.day != 1:
            raise DailyError(f"{series}: not a monthly series ({day})")
        value = row[1].strip()
        if value in ("", "."):
            out[(day.year, day.month)] = None
        else:
            try:
                out[(day.year, day.month)] = float(value)
            except ValueError as error:
                raise DailyError(f"{series}: bad value {value}") from error
    return out
