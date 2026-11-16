"""Point-in-time archive of calendar, COT and retail sentiment snapshots.

Every run fetches each source once and appends the raw answer, with the UTC
time it was received, to ``<root>/<source>/<YYYY>/<YYYY-MM-DD>.jsonl``. Nothing
is ever rewritten: a value revised later shows up as a new record, so a backtest
can always ask "what was known at time t". A record is skipped when the body is
identical to the last one stored for that source (no new information).

Bodies are kept raw (decoded JSON when possible, text otherwise) so that a
change in a provider's format never loses data; parsing happens later, in M4.
Credentials are read from the environment and never written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ARCHIVE_VERSION = "news-archive-v1"
USER_AGENT = "fxnext-news-archive/1 (research; low frequency)"

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# CFTC public reporting (Socrata). Traders in Financial Futures, futures only.
COT_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
# CFTC contract market codes: EURO FX, BRITISH POUND, JAPANESE YEN (CME)
COT_CODES = {"EUR": "099741", "GBP": "096742", "JPY": "097741"}
MYFXBOOK_API = "https://www.myfxbook.com/api"
MYFXBOOK_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY")
EMAIL_ENV = "FXNEXT_MYFXBOOK_EMAIL"
PASSWORD_ENV = "FXNEXT_MYFXBOOK_PASSWORD"

Fetcher = Callable[[str], tuple[int, bytes]]


class SourceError(RuntimeError):
    pass


def http_get(url: str, timeout: float = 30.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed hosts)
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except OSError as exc:
        raise SourceError(f"rete: {type(exc).__name__}") from None


@dataclass(frozen=True)
class Snapshot:
    source: str
    url: str  # never contains credentials or session tokens
    status: int
    body: bytes


def _decode(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def fetch_calendar(get: Fetcher) -> Snapshot:
    status, body = get(CALENDAR_URL)
    return Snapshot("calendar", CALENDAR_URL, status, body)


def cot_url(limit: int = 12) -> str:
    codes = ",".join(f"'{c}'" for c in COT_CODES.values())
    query = {
        "$where": f"cftc_contract_market_code in({codes})",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(limit),
    }
    return COT_URL + "?" + urllib.parse.urlencode(query)


def fetch_cot(get: Fetcher) -> Snapshot:
    url = cot_url()
    status, body = get(url)
    return Snapshot("cot", url, status, body)


def fetch_sentiment(get: Fetcher, env: Mapping[str, str]) -> Snapshot:
    email, password = env.get(EMAIL_ENV, "").strip(), env.get(PASSWORD_ENV, "").strip()
    if not email or not password:
        raise SourceError(f"imposta {EMAIL_ENV} e {PASSWORD_ENV}")
    login = f"{MYFXBOOK_API}/login.json?" + urllib.parse.urlencode(
        {"email": email, "password": password}
    )
    status, body = get(login)
    answer = _decode(body)
    if (
        status != 200
        or not isinstance(answer, dict)
        or answer.get("error")
        or not answer.get("session")
    ):
        message = answer.get("message", "") if isinstance(answer, dict) else ""
        raise SourceError(f"login Myfxbook non riuscito ({status}) {message}".strip())
    session = str(answer["session"])
    public_url = f"{MYFXBOOK_API}/get-community-outlook.json"
    try:
        status, body = get(public_url + "?" + urllib.parse.urlencode({"session": session}))
    finally:
        with contextlib.suppress(SourceError):
            get(f"{MYFXBOOK_API}/logout.json?" + urllib.parse.urlencode({"session": session}))
    data = _decode(body)
    if isinstance(data, dict) and isinstance(data.get("symbols"), list):
        data["symbols"] = [s for s in data["symbols"] if s.get("name") in MYFXBOOK_SYMBOLS]
        body = json.dumps(data, sort_keys=True).encode()
    return Snapshot("sentiment", public_url, status, body)


def _day_file(root: Path, source: str, at: datetime) -> Path:
    return root / source / f"{at:%Y}" / f"{at:%Y-%m-%d}.jsonl"


def _last_sha(root: Path, source: str) -> str | None:
    folder = root / source
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*/*.jsonl"))
    for path in reversed(files):
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            try:
                return str(json.loads(lines[-1]).get("sha256"))
            except ValueError:
                return None
    return None


def store(root: Path, snap: Snapshot, received_at: datetime) -> bool:
    """Append one record; return False when the body is unchanged since the last record."""

    sha = hashlib.sha256(snap.body).hexdigest()
    if snap.status == 200 and _last_sha(root, snap.source) == sha:
        return False
    record = {
        "archive_version": ARCHIVE_VERSION,
        "source": snap.source,
        "received_at": received_at.astimezone(UTC).isoformat(),
        "url": snap.url,
        "http_status": snap.status,
        "sha256": sha,
        "body": _decode(snap.body),
    }
    path = _day_file(root, snap.source, received_at.astimezone(UTC))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return True


SOURCES = ("calendar", "cot", "sentiment")


def archive_once(
    root: Path,
    sources: tuple[str, ...] = SOURCES,
    *,
    get: Fetcher = http_get,
    env: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> dict[str, str]:
    """Fetch every source once. Returns {source: "saved" | "unchanged" | "error: ..."}."""

    env = dict(os.environ) if env is None else env
    result: dict[str, str] = {}
    for name in sources:
        try:
            if name == "calendar":
                snap = fetch_calendar(get)
            elif name == "cot":
                snap = fetch_cot(get)
            elif name == "sentiment":
                snap = fetch_sentiment(get, env)
            else:
                raise SourceError("fonte sconosciuta")
            saved = store(root, snap, now())
            if snap.status != 200:
                result[name] = f"error: HTTP {snap.status} (risposta salvata)"
            else:
                result[name] = "saved" if saved else "unchanged"
        except SourceError as exc:
            result[name] = f"error: {exc}"
    return result


def archive_status(root: Path) -> list[dict[str, Any]]:
    """Last record and record count per source, for the web app."""

    out: list[dict[str, Any]] = []
    for name in SOURCES:
        folder = root / name
        files = sorted(folder.glob("*/*.jsonl")) if folder.is_dir() else []
        count, last = 0, None
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    count += 1
                    last = line
        rec = json.loads(last) if last else {}
        out.append(
            {
                "source": name,
                "records": count,
                "days": len(files),
                "last_received": rec.get("received_at", ""),
                "last_status": rec.get("http_status"),
            }
        )
    return out


__all__ = [
    "ARCHIVE_VERSION",
    "SOURCES",
    "Snapshot",
    "SourceError",
    "archive_once",
    "archive_status",
    "cot_url",
    "store",
]
