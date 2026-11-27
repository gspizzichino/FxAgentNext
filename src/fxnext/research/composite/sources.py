"""Free sources of the l1-composito-001 filter and their download (M10).

Everything is fixed here, before any value is read: the FRED series of each
currency and variable (with fallbacks in a fixed order), the daily Deutsche Mark
from the Bundesbank (Frankfurt fixing, FRED has no daily DEM), and the cut-off.
The downloader asks FRED only for observations up to ``LAST_DAY``: no value after
2003 reaches the disk, so the reserved 2014-2026 data cannot leak in.
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fxnext.data.daily import RatesCache, _fetch, fred_fetcher
from fxnext.data.dukascopy import Fetcher, FetchStatus

L1_DOWNLOADER_VERSION = "l1-sources-downloader-v1"
FIRST_DAY = date(1970, 1, 1)
LAST_DAY = date(2003, 12, 31)
FRED_URL = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    f"&cosd={FIRST_DAY.isoformat()}&coed={LAST_DAY.isoformat()}"
)
BUNDESBANK_URL = (
    "https://api.statistiken.bundesbank.de/rest/data/BBEX3/{key}?format=csv&lang=en"
    f"&startPeriod={FIRST_DAY.isoformat()}&endPeriod=1998-12-31"
)
DEM_SERIES = "BBEX3.D.USD.DEM.AA.AC.000"  # Frankfurt fixing, USD 1 = DEM ..., to 1998
DEM_PER_EUR = 1.95583  # irrevocable conversion rate, 31/12/1998

CURRENCIES = ("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK")
VARIABLES = ("cpi", "ip", "retail", "unemp")
SIGNS = {"cpi": 1.0, "ip": 1.0, "retail": 1.0, "unemp": -1.0}

# (FRED id, True when the quote is USD per unit of foreign currency).
FX_SERIES: dict[str, tuple[str, bool]] = {
    "EUR": ("DEXUSEU", True),  # from 1999-01; DEM before (Bundesbank)
    "GBP": ("DEXUSUK", True),
    "JPY": ("DEXJPUS", False),
    "CHF": ("DEXSZUS", False),
    "CAD": ("DEXCAUS", False),
    "AUD": ("DEXUSAL", True),
    "NZD": ("DEXUSNZ", True),
    "SEK": ("DEXSDUS", False),
    "NOK": ("DEXNOUS", False),
}

ISO = {
    "USD": ("US", "USA"),
    "EUR": ("DE", "DEU"),  # Germany for the whole period (Giacomo, 2026-11-26)
    "GBP": ("GB", "GBR"),
    "JPY": ("JP", "JPN"),
    "CHF": ("CH", "CHE"),
    "CAD": ("CA", "CAN"),
    "AUD": ("AU", "AUS"),
    "NZD": ("NZ", "NZL"),
    "SEK": ("SE", "SWE"),
    "NOK": ("NO", "NOR"),
}

MACRO_ORDER = ("USD",) + CURRENCIES


def macro_candidates(ccy: str, variable: str) -> tuple[str, ...]:
    """FRED ids in a fixed order: the loader keeps the one with the earliest data."""

    c2, c3 = ISO[ccy]
    if variable == "cpi":
        return (f"{c3}CPIALLMINMEI", f"{c3}CPIALLQINMEI")
    if variable == "ip":
        return (f"{c3}PROINDMISMEI", f"{c3}PROINDQISMEI")
    if variable == "retail":
        return (
            f"{c3}SLRTTO02IXOBSAM",
            f"{c3}SLRTTO01IXOBSAM",
            f"{c3}SLRTTO02IXOBSAQ",
            f"{c3}SLRTTO01IXOBSAQ",
        )
    if variable == "unemp":
        return (
            f"LRHUTTTT{c2}M156S",
            f"LRUNTTTT{c2}M156S",
            f"LRHUTTTT{c2}Q156S",
            f"LRUNTTTT{c2}Q156S",
        )
    raise ValueError(variable)


# 3-month rates: (FRED id, last month it is used or None). Substitutes decided by
# Giacomo on 2026-11-26: Japanese 3-month CD to March 2002, Swiss call money to June
# 1999, then the OECD interbank rate. EUR uses the German rate (as the macro data).
RATE_SERIES: dict[str, tuple[tuple[str, tuple[int, int] | None], ...]] = {
    "USD": (("IR3TIB01USM156N", None),),
    "EUR": (("IR3TIB01DEM156N", None),),
    "GBP": (("IR3TIB01GBM156N", None),),
    "JPY": (("IR3TCD01JPM156N", (2002, 3)), ("IR3TIB01JPM156N", None)),
    "CHF": (("IRSTCI01CHM156N", (1999, 6)), ("IR3TIB01CHM156N", None)),
    "CAD": (("IR3TIB01CAM156N", None),),
    "AUD": (("IR3TIB01AUM156N", None),),
    "NZD": (("IR3TIB01NZM156N", None),),
    "SEK": (("IR3TIB01SEM156N", None),),
    "NOK": (("IR3TIB01NOM156N", None),),
}


def all_fred_series() -> list[str]:
    out: list[str] = [sid for sid, _ in FX_SERIES.values()]
    for ccy in MACRO_ORDER:
        for var in VARIABLES:
            out.extend(macro_candidates(ccy, var))
        out.extend(sid for sid, _ in RATE_SERIES[ccy])
    return list(dict.fromkeys(out))


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Series:
    sid: str
    obs: dict[date, float]  # missing values dropped
    frequency: str  # "D", "M" or "Q"


def _frequency(days: list[date]) -> str:
    if not days:
        return "?"
    if any(d.day != 1 for d in days):
        return "D"
    months = {d.month for d in days}
    return "Q" if months <= {1, 4, 7, 10} and len(days) > 4 else "M"


def parse_fred(payload: bytes, sid: str) -> Series:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SourceError(f"{sid}: not UTF-8") from error
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None or len(header) != 2 or header[1].strip() != sid:
        raise SourceError(f"{sid}: unexpected header {header}")
    obs: dict[date, float] = {}
    days: list[date] = []
    for row in reader:
        if not row:
            continue
        if len(row) != 2:
            raise SourceError(f"{sid}: bad row {row}")
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError as error:
            raise SourceError(f"{sid}: bad date {row[0]}") from error
        days.append(day)
        value = row[1].strip()
        if value in ("", "."):
            continue
        try:
            obs[day] = float(value)
        except ValueError as error:
            raise SourceError(f"{sid}: bad value {value!r}") from error
    return Series(sid, obs, _frequency(days))


def parse_bundesbank(payload: bytes, sid: str) -> Series:
    """Bundesbank CSV: metadata lines, then ``YYYY-MM-DD,value,flag``."""

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SourceError(f"{sid}: not UTF-8") from error
    lines = text.splitlines()
    if not lines or sid not in lines[0]:
        raise SourceError(f"{sid}: unexpected header")
    obs: dict[date, float] = {}
    for row in csv.reader(lines[1:]):
        if not row:
            continue
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError:
            continue  # metadata line
        value = row[1].strip() if len(row) > 1 else ""
        try:
            obs[day] = float(value)
        except ValueError:
            continue  # "." or empty: no fixing that day
    if not obs:
        raise SourceError(f"{sid}: no observations")
    return Series(sid, obs, "D")


def download_sources(
    root: Path,
    *,
    fetcher: Fetcher = fred_fetcher,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_attempts: int = 4,
    backoff_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 30.0,
    min_interval: float = 2.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, str]:
    """Fetch every series once into ``root`` (``{id}.{sha12}.csv`` + fetch log).

    A FRED id that does not exist answers 404: it is logged NOT_FOUND and the
    loader uses the next candidate. Already resolved ids are skipped, so a run
    interrupted by FRED's rate limit can simply be repeated.
    """

    cache = RatesCache(root)
    resolved: dict[str, str] = {}
    for rec in cache.records():
        if rec["status"] in (FetchStatus.OK, FetchStatus.NOT_FOUND):
            resolved[rec["series"]] = rec["status"]
    jobs = [(sid, FRED_URL.format(series=sid)) for sid in all_fred_series()]
    jobs.append((DEM_SERIES, BUNDESBANK_URL.format(key=DEM_SERIES.removeprefix("BBEX3."))))
    out: dict[str, str] = {}
    first = True
    for sid, url in jobs:
        if sid in resolved:
            out[sid] = "SKIPPED"
            continue
        if not first and min_interval > 0:
            sleep(min_interval)
        first = False
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
            "downloader_version": L1_DOWNLOADER_VERSION,
            "detail": error,
        }
        if result is not None and result.status in (400, 404):
            record["status"] = FetchStatus.NOT_FOUND
        elif result is None or result.status != 200 or not result.body:
            record["status"] = FetchStatus.FAILED
        else:
            try:
                parsed = (
                    parse_bundesbank(result.body, sid)
                    if sid == DEM_SERIES
                    else parse_fred(result.body, sid)
                )
                late = [d for d in parsed.obs if d > LAST_DAY]
                if late:
                    raise SourceError(f"{sid}: {len(late)} observations after {LAST_DAY}")
            except SourceError as exc:
                record.update(status=FetchStatus.CORRUPT, detail=str(exc))
            else:
                path = cache.store(sid, result.body)
                record.update(
                    status=FetchStatus.OK,
                    sha256=hashlib.sha256(result.body).hexdigest(),
                    cache_path=path.relative_to(cache.root).as_posix(),
                )
        cache.append(record)
        out[sid] = str(record["status"])
        if progress is not None:
            progress(record)
    return out


def load_series(root: Path, sid: str) -> tuple[Series, str] | None:
    """The cached file of ``sid`` checked against its hash, or None if absent."""

    files = sorted(Path(root).glob(f"{sid}.*.csv"))
    if not files:
        return None
    if len(files) != 1:
        raise SourceError(f"{sid}: {len(files)} files in {root}")
    payload = files[0].read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if not files[0].name.startswith(f"{sid}.{digest[:12]}"):
        raise SourceError(f"{files[0].name}: content does not match its hash")
    series = parse_bundesbank(payload, sid) if sid == DEM_SERIES else parse_fred(payload, sid)
    late = [d for d in series.obs if d > LAST_DAY]
    if late:
        raise SourceError(f"{sid}: observations after {LAST_DAY} on disk")
    return series, digest
