"""News archive: append-only, point-in-time, no credentials on disk."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fxnext.news import archive as na

EMAIL, PASSWORD, SESSION = "me@example.com", "s3cret-pw", "SESSIONTOKEN123"


class FakeWeb:
    def __init__(self) -> None:
        self.calendar = b'[{"title":"CPI m/m","country":"USD","impact":"High","forecast":"0.3%"}]'
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        if url == na.CALENDAR_URL:
            return 200, self.calendar
        if url.startswith(na.COT_URL):
            return 200, b'[{"report_date_as_yyyy_mm_dd":"2026-11-10T00:00:00.000"}]'
        if "/login.json" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            ok = q.get("password") == [PASSWORD]
            body = {
                "error": not ok,
                "message": "" if ok else "bad",
                "session": SESSION if ok else "",
            }
            return 200, json.dumps(body).encode()
        if "/get-community-outlook.json" in url:
            symbols = [
                {"name": "EURUSD", "longPercentage": 38, "shortPercentage": 62},
                {"name": "XAUUSD", "longPercentage": 70, "shortPercentage": 30},
            ]
            return 200, json.dumps({"error": False, "symbols": symbols}).encode()
        if "/logout.json" in url:
            return 200, b'{"error":false}'
        return 404, b"not found"


ENV = {na.EMAIL_ENV: EMAIL, na.PASSWORD_ENV: PASSWORD}


def _clock(start: datetime):  # type: ignore[no-untyped-def]
    t = [start]

    def now() -> datetime:
        t[0] += timedelta(minutes=30)
        return t[0]

    return now


def _records(root: Path, source: str) -> list[dict[str, object]]:
    out = []
    for path in sorted((root / source).glob("*/*.jsonl")):
        out += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return out


def test_archive_saves_then_skips_unchanged_then_saves_revision(tmp_path: Path) -> None:
    web = FakeWeb()
    now = _clock(datetime(2026, 11, 16, 18, 0, tzinfo=UTC))
    assert na.archive_once(tmp_path, get=web, env=ENV, now=now) == {
        "calendar": "saved",
        "cot": "saved",
        "sentiment": "saved",
    }
    assert na.archive_once(tmp_path, get=web, env=ENV, now=now)["calendar"] == "unchanged"
    web.calendar = web.calendar.replace(b"0.3%", b"0.2%")  # forecast revised
    assert na.archive_once(tmp_path, get=web, env=ENV, now=now)["calendar"] == "saved"
    recs = _records(tmp_path, "calendar")
    assert [r["body"][0]["forecast"] for r in recs] == ["0.3%", "0.2%"]  # type: ignore[index]
    assert recs[0]["received_at"] < recs[1]["received_at"]  # type: ignore[operator]
    assert recs[0]["archive_version"] == na.ARCHIVE_VERSION


def test_credentials_and_session_never_reach_disk(tmp_path: Path) -> None:
    web = FakeWeb()
    na.archive_once(tmp_path, ("sentiment",), get=web, env=ENV)
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.jsonl"))
    for secret in (EMAIL, PASSWORD, SESSION):
        assert secret not in text
    rec = _records(tmp_path, "sentiment")[0]
    assert [s["name"] for s in rec["body"]["symbols"]] == ["EURUSD"]  # type: ignore[index]
    assert any("/logout.json" in c for c in web.calls)


def test_sentiment_errors_are_reported_not_raised(tmp_path: Path) -> None:
    web = FakeWeb()
    out = na.archive_once(tmp_path, ("sentiment",), get=web, env={})
    assert out["sentiment"].startswith("error") and na.EMAIL_ENV in out["sentiment"]
    bad = {na.EMAIL_ENV: EMAIL, na.PASSWORD_ENV: "wrong"}
    assert na.archive_once(tmp_path, ("sentiment",), get=web, env=bad)["sentiment"].startswith(
        "error: login"
    )
    assert not list(tmp_path.rglob("*.jsonl"))


def test_http_errors_are_kept_as_records(tmp_path: Path) -> None:
    def down(url: str) -> tuple[int, bytes]:
        return 503, b"busy"

    out = na.archive_once(tmp_path, ("calendar",), get=down, env={})
    assert out["calendar"].startswith("error: HTTP 503")
    assert _records(tmp_path, "calendar")[0]["http_status"] == 503


def test_cot_query_asks_for_the_three_currencies() -> None:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(na.cot_url()).query)
    for code in na.COT_CODES.values():
        assert code in q["$where"][0]


def test_status_for_the_web_app(tmp_path: Path) -> None:
    na.archive_once(tmp_path, ("calendar",), get=FakeWeb(), env={})
    status = {s["source"]: s for s in na.archive_status(tmp_path)}
    assert status["calendar"]["records"] == 1 and status["calendar"]["last_status"] == 200
    assert status["cot"]["records"] == 0
