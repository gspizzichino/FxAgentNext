"""Dukascopy downloader with an immutable raw cache (ADR-0015, contracts §13).

Only ``download_range`` touches the network. Every finished attempt appends one
``RawFetchRecord`` line to ``fetch_log.jsonl``. Cached files are never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fxnext.data import bi5
from fxnext.domain.enums import PriceBasis

DOWNLOADER_VERSION = "dukascopy-downloader-v1"
DEFAULT_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
USER_AGENT = "fxnext-research/0.1"


class FetchStatus:
    OK = "OK"
    EMPTY = "EMPTY"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"
    CORRUPT = "CORRUPT"
    SOURCE_CHANGED = "SOURCE_CHANGED"


def day_url(base_url: str, symbol: str, basis: PriceBasis, day: date) -> str:
    return (
        f"{base_url.rstrip('/')}/{symbol}/{day.year:04d}/{day.month - 1:02d}/"
        f"{day.day:02d}/{basis.value}_candles_min_1.bi5"
    )


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    body: bytes


class TransientHttpError(Exception):
    pass


Fetcher = Callable[[str, float], HttpResult]


def urllib_fetcher(url: str, timeout: float) -> HttpResult:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HttpResult(int(response.status), response.read())
    except urllib.error.HTTPError as error:
        if error.code >= 500 or error.code == 429:  # 429: source rate limit, retry later
            raise TransientHttpError(f"HTTP {error.code}") from error
        return HttpResult(int(error.code), b"")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise TransientHttpError(str(error)) from error


class RawCache:
    """``root/{SYMBOL}/{BASIS}/{YYYY}/{YYYY-MM-DD}.{sha12}.bi5`` + ``fetch_log.jsonl``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.log_path = self.root / "fetch_log.jsonl"

    def day_dir(self, symbol: str, basis: PriceBasis, day: date) -> Path:
        return self.root / symbol / basis.value / f"{day.year:04d}"

    def records(self) -> Iterator[dict[str, Any]]:
        if not self.log_path.exists():
            return
        with self.log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def resolved(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        """First terminal record (OK/EMPTY/NOT_FOUND) per (symbol, basis, day)."""

        out: dict[tuple[str, str, str], dict[str, Any]] = {}
        for rec in self.records():
            key = (rec["symbol"], rec["basis"], rec["day"])
            if key not in out and rec["status"] in (
                FetchStatus.OK,
                FetchStatus.EMPTY,
                FetchStatus.NOT_FOUND,
            ):
                out[key] = rec
        return out

    def append(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def store(self, symbol: str, basis: PriceBasis, day: date, payload: bytes) -> Path:
        digest = hashlib.sha256(payload).hexdigest()
        directory = self.day_dir(symbol, basis, day)
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{day.isoformat()}.{digest[:12]}.bi5"
        if final.exists():
            return final
        tmp = directory / f".tmp-{day.isoformat()}-{os.getpid()}"
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
        return final


@dataclass(frozen=True, slots=True)
class DownloadSummary:
    requested: int
    skipped: int
    ok: int
    empty: int
    not_found: int
    failed: int
    corrupt: int
    source_changed: int


def iter_days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def download_range(
    cache: RawCache,
    symbols: list[str],
    bases: list[PriceBasis],
    start: date,
    end: date,
    *,
    base_url: str = DEFAULT_BASE_URL,
    fetcher: Fetcher = urllib_fetcher,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_attempts: int = 4,
    backoff_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 30.0,
    margin: timedelta = timedelta(hours=2),
    refetch: bool = False,
    min_interval: float = 0.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> DownloadSummary:
    """Download whole UTC days into the cache, skipping days already resolved."""

    counts = dict.fromkeys(
        ("requested", "skipped", "ok", "empty", "not_found", "failed", "corrupt", "changed"), 0
    )
    resolved = cache.resolved()
    current = now()
    first_request = True
    for symbol in symbols:
        for basis in bases:
            for day in iter_days(start, end):
                day_end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
                if day_end + margin > current:
                    continue  # T8.8: never fetch a day that has not fully elapsed
                key = (symbol, basis.value, day.isoformat())
                previous = resolved.get(key)
                if previous is not None and not refetch:
                    counts["skipped"] += 1
                    continue
                counts["requested"] += 1
                if not first_request and min_interval > 0:
                    sleep(min_interval)  # be polite: the source rate-limits bursts (HTTP 429)
                first_request = False
                record = _fetch_day(
                    cache,
                    symbol,
                    basis,
                    day,
                    base_url,
                    fetcher,
                    max_attempts,
                    backoff_seconds,
                    sleep,
                    timeout,
                    now,
                )
                if (
                    previous is not None
                    and record["status"] == FetchStatus.OK
                    and previous["status"] == FetchStatus.OK
                    and previous["sha256"] != record["sha256"]
                ):
                    record = {**record, "status": FetchStatus.SOURCE_CHANGED}
                cache.append(record)
                if progress is not None:
                    progress(record)
                status = record["status"]
                counts[
                    {
                        FetchStatus.OK: "ok",
                        FetchStatus.EMPTY: "empty",
                        FetchStatus.NOT_FOUND: "not_found",
                        FetchStatus.FAILED: "failed",
                        FetchStatus.CORRUPT: "corrupt",
                        FetchStatus.SOURCE_CHANGED: "changed",
                    }[status]
                ] += 1
    return DownloadSummary(
        counts["requested"],
        counts["skipped"],
        counts["ok"],
        counts["empty"],
        counts["not_found"],
        counts["failed"],
        counts["corrupt"],
        counts["changed"],
    )


def _fetch_day(
    cache: RawCache,
    symbol: str,
    basis: PriceBasis,
    day: date,
    base_url: str,
    fetcher: Fetcher,
    max_attempts: int,
    backoff: float,
    sleep: Callable[[float], None],
    timeout: float,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    url = day_url(base_url, symbol, basis, day)
    attempts = 0
    result: HttpResult | None = None
    error = ""
    while attempts < max_attempts:
        attempts += 1
        try:
            result = fetcher(url, timeout)
            break
        except TransientHttpError as exc:
            error = str(exc)
            if attempts < max_attempts:
                sleep(backoff * (2 ** (attempts - 1)))
    record: dict[str, Any] = {
        "url": url,
        "symbol": symbol,
        "basis": basis.value,
        "day": day.isoformat(),
        "http_status": None if result is None else result.status,
        "attempts": attempts,
        "bytes": 0,
        "sha256": None,
        "cache_path": None,
        "fetched_at": now().isoformat(),
        "downloader_version": DOWNLOADER_VERSION,
        "parser_version": bi5.PARSER_VERSION,
        "detail": error,
    }
    if result is None:
        return {**record, "status": FetchStatus.FAILED}
    if result.status == 404:
        return {**record, "status": FetchStatus.NOT_FOUND}
    if result.status != 200:
        return {**record, "status": FetchStatus.FAILED, "detail": f"HTTP {result.status}"}
    if not result.body:
        return {**record, "status": FetchStatus.EMPTY}
    try:
        bi5.decode(result.body)
    except bi5.Bi5Error as exc:
        return {
            **record,
            "status": FetchStatus.CORRUPT,
            "bytes": len(result.body),
            "detail": str(exc),
        }
    path = cache.store(symbol, basis, day, result.body)
    return {
        **record,
        "status": FetchStatus.OK,
        "bytes": len(result.body),
        "sha256": hashlib.sha256(result.body).hexdigest(),
        "cache_path": path.relative_to(cache.root).as_posix(),
    }
