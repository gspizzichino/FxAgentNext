"""T8: Dukascopy downloader, raw cache and M1 -> M15 import, against a local HTTP server."""

from __future__ import annotations

import http.server
import threading
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from fxnext.data import bi5
from fxnext.data.dukascopy import FetchStatus, RawCache, day_url, download_range
from fxnext.data.importers import cross_check_bid_ask, import_dukascopy
from fxnext.data.store import write_dataset
from fxnext.data.synthetic import synthetic_bi5_day
from fxnext.domain.enums import DatasetProvenance, PriceBasis, RejectReason, SlotStatus

BID, ASK = PriceBasis.BID, PriceBasis.ASK
NOW = lambda: datetime(2024, 3, 1, tzinfo=UTC)  # noqa: E731


class Server:
    def __init__(self) -> None:
        self.routes: dict[str, list[tuple[int, bytes]]] = {}
        self.hits: Counter[str] = Counter()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits[self.path] += 1
                queue = owner.routes.get(self.path, [(404, b"")])
                status, body = queue.pop(0) if len(queue) > 1 else queue[0]
                if status == -1:  # truncated: announce more bytes than sent
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body) + 100))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/datafeed"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def path(self, symbol: str, basis: PriceBasis, day: date) -> str:
        return day_url("/datafeed", symbol, basis, day)


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    yield s
    s.httpd.shutdown()


def _serve_days(server: Server, symbol: str, days: list[date], ask_offset: int = 2) -> None:
    for i, d in enumerate(days):
        bid = synthetic_bi5_day(d, seed=i)
        server.routes[server.path(symbol, BID, d)] = [(200, bid)]
        candles = [
            bi5.MinuteCandle(
                c.second_of_day,
                c.open + ask_offset,
                c.close + ask_offset,
                c.low + ask_offset,
                c.high + ask_offset,
                c.volume,
            )
            for c in bi5.decode(bid)
        ]
        server.routes[server.path(symbol, ASK, d)] = [(200, bi5.encode(candles))]


def _dl(server: Server, cache: RawCache, days: list[date], **kw: object):  # type: ignore[no-untyped-def]
    return download_range(
        cache,
        ["EURUSD"],
        [BID, ASK],
        days[0],
        days[-1],
        base_url=server.base,
        now=NOW,
        sleep=lambda s: None,
        **kw,
    )  # type: ignore[arg-type]


DAYS = [date(2024, 2, 6), date(2024, 2, 7), date(2024, 2, 8)]


def test_t8_1_download_and_import(server: Server, tmp_path: Path) -> None:
    _serve_days(server, "EURUSD", DAYS)
    cache = RawCache(tmp_path / "raw")
    summary = _dl(server, cache, DAYS)
    assert summary.ok == 6 and summary.requested == 6
    records = list(cache.records())
    assert len(records) == 6 and all(r["sha256"] and r["bytes"] for r in records)
    assert len(list((tmp_path / "raw").rglob("*.bi5"))) == 6
    bid, raw = import_dukascopy(cache, "EURUSD", BID, DAYS[0], DAYS[-1])
    ask, _ = import_dukascopy(cache, "EURUSD", ASK, DAYS[0], DAYS[-1])
    cross_check_bid_ask(bid, ask)
    assert len(raw) == 3 and len(bid.rows) == 3 * 96 and not bid.rejected
    first = bi5.decode(server.routes[server.path("EURUSD", BID, DAYS[0])][0][1])[:15]
    row = sorted(bid.rows, key=lambda r: r.open_time_ns)[0]
    assert row.open == first[0].open / 1e5 and row.close == first[-1].close / 1e5
    assert row.high == max(c.high for c in first) / 1e5
    assert row.low == min(c.low for c in first) / 1e5


def test_t8_2_urls_and_digits() -> None:
    assert (
        day_url("https://x/datafeed", "EURUSD", BID, date(2024, 1, 31))
        == "https://x/datafeed/EURUSD/2024/00/31/BID_candles_min_1.bi5"
    )
    assert (
        day_url("https://x/datafeed", "USDJPY", ASK, date(2024, 12, 1))
        == "https://x/datafeed/USDJPY/2024/11/01/ASK_candles_min_1.bi5"
    )
    assert bi5.PRICE_DIGITS == {"EURUSD": 5, "GBPUSD": 5, "USDJPY": 3}


def test_t8_2_usdjpy_scale(server: Server, tmp_path: Path) -> None:
    d = DAYS[0]
    server.routes[server.path("USDJPY", BID, d)] = [
        (200, synthetic_bi5_day(d, seed=1, start_points=150_000))
    ]
    cache = RawCache(tmp_path / "raw")
    download_range(cache, ["USDJPY"], [BID], d, d, base_url=server.base, now=NOW)
    res, _ = import_dukascopy(cache, "USDJPY", BID, d, d)
    assert 149.0 < res.rows[0].open < 151.0


def test_t8_3_empty_and_404(server: Server, tmp_path: Path) -> None:
    server.routes[server.path("EURUSD", BID, DAYS[0])] = [(200, b"")]
    cache = RawCache(tmp_path / "raw")
    s = download_range(cache, ["EURUSD"], [BID], DAYS[0], DAYS[1], base_url=server.base, now=NOW)
    assert (s.empty, s.not_found) == (1, 1)
    res, _ = import_dukascopy(cache, "EURUSD", BID, DAYS[0], DAYS[1])
    assert not res.rows and res.quality["days_empty"] == 1 and res.quality["days_not_found"] == 1


def test_t8_4_retries(server: Server, tmp_path: Path) -> None:
    body = synthetic_bi5_day(DAYS[0], seed=3)
    p = server.path("EURUSD", BID, DAYS[0])
    server.routes[p] = [(500, b""), (500, b""), (200, body)]
    sleeps: list[float] = []
    cache = RawCache(tmp_path / "raw")
    s = download_range(
        cache,
        ["EURUSD"],
        [BID],
        DAYS[0],
        DAYS[0],
        base_url=server.base,
        now=NOW,
        sleep=sleeps.append,
        backoff_seconds=0.5,
    )
    assert s.ok == 1 and server.hits[p] == 3 and sleeps == [0.5, 1.0]
    p2 = server.path("EURUSD", BID, DAYS[1])
    server.routes[p2] = [(503, b"")]
    s2 = download_range(
        cache,
        ["EURUSD"],
        [BID],
        DAYS[1],
        DAYS[1],
        base_url=server.base,
        now=NOW,
        sleep=lambda x: None,
    )
    assert s2.failed == 1 and server.hits[p2] == 4
    from fxnext.cli import main  # noqa: F401  (exit code checked via summary.failed)


def test_t8_5_corrupt(server: Server, tmp_path: Path) -> None:
    server.routes[server.path("EURUSD", BID, DAYS[0])] = [(200, b"garbage-not-lzma")]
    good = synthetic_bi5_day(DAYS[1], seed=1)
    server.routes[server.path("EURUSD", BID, DAYS[1])] = [(200, good[: len(good) - 7])]
    cache = RawCache(tmp_path / "raw")
    s = download_range(cache, ["EURUSD"], [BID], DAYS[0], DAYS[1], base_url=server.base, now=NOW)
    assert s.corrupt == 2
    assert not list((tmp_path / "raw").rglob("*.bi5"))
    assert not list((tmp_path / "raw").rglob(".tmp-*"))


def test_t8_6_resume(server: Server, tmp_path: Path) -> None:
    _serve_days(server, "EURUSD", DAYS)
    cache = RawCache(tmp_path / "raw")
    _dl(server, cache, DAYS[:2])
    total = sum(server.hits.values())
    _dl(server, cache, DAYS)
    assert sum(server.hits.values()) - total == 2  # only the third day, BID + ASK


def test_t8_7_source_changed(server: Server, tmp_path: Path) -> None:
    p = server.path("EURUSD", BID, DAYS[0])
    server.routes[p] = [(200, synthetic_bi5_day(DAYS[0], seed=1))]
    cache = RawCache(tmp_path / "raw")
    download_range(cache, ["EURUSD"], [BID], DAYS[0], DAYS[0], base_url=server.base, now=NOW)
    before, _ = import_dukascopy(cache, "EURUSD", BID, DAYS[0], DAYS[0])
    server.routes[p] = [(200, synthetic_bi5_day(DAYS[0], seed=2))]
    s = download_range(
        cache, ["EURUSD"], [BID], DAYS[0], DAYS[0], base_url=server.base, now=NOW, refetch=True
    )
    assert s.source_changed == 1
    assert len(list((tmp_path / "raw").rglob("*.bi5"))) == 2
    after, _ = import_dukascopy(cache, "EURUSD", BID, DAYS[0], DAYS[0])
    assert [r.close for r in before.rows] == [r.close for r in after.rows]  # first OK is used


def test_t8_8_unfinished_days_skipped(server: Server, tmp_path: Path) -> None:
    cache = RawCache(tmp_path / "raw")
    now = lambda: datetime(2024, 2, 8, 1, tzinfo=UTC)  # noqa: E731
    s = download_range(cache, ["EURUSD"], [BID], DAYS[0], DAYS[2], base_url=server.base, now=now)
    assert s.requested == 1  # Feb 6 only; Feb 7 ends < 2h ago, Feb 8 not finished
    assert server.hits[server.path("EURUSD", BID, DAYS[2])] == 0


def test_t8_9_minute_rules(server: Server, tmp_path: Path) -> None:
    d = DAYS[0]
    candles = list(bi5.decode(synthetic_bi5_day(d, seed=5)))
    del candles[10 * 60 + 3]  # 10:03 missing -> M15 10:00 INVALID
    for m in range(11 * 60, 11 * 60 + 15):  # 11:00-11:15 all flat zero volume
        c = candles[m - 1]
        candles[m - 1] = bi5.MinuteCandle(c.second_of_day, 110_000, 110_000, 110_000, 110_000, 0.0)
    server.routes[server.path("EURUSD", BID, d)] = [(200, bi5.encode(candles))]
    ask = [
        bi5.MinuteCandle(c.second_of_day, c.open + 2, c.close + 2, c.low + 2, c.high + 2, c.volume)
        for c in candles
    ]
    i = 12 * 60 - 1  # minute 12:00 (one earlier minute was deleted)
    ask[i] = bi5.MinuteCandle(
        ask[i].second_of_day,
        candles[i].open - 5,
        ask[i].close,
        candles[i].open - 6,
        ask[i].high,
        1.0,
    )  # 12:00 ask open below bid open
    server.routes[server.path("EURUSD", ASK, d)] = [(200, bi5.encode(ask))]
    cache = RawCache(tmp_path / "raw")
    _dl(server, cache, [d])
    bid, _ = import_dukascopy(cache, "EURUSD", BID, d, d)
    askr, _ = import_dukascopy(cache, "EURUSD", ASK, d, d)
    cross_check_bid_ask(bid, askr)
    t = lambda h, m: int(datetime(2024, 2, 6, h, m, tzinfo=UTC).timestamp()) * 10**9  # noqa: E731
    reasons = {r.open_time_ns: r.reason for r in bid.rejected}
    assert reasons[t(10, 0)] is RejectReason.INCOMPLETE_SOURCE
    assert reasons[t(12, 0)] is RejectReason.NEGATIVE_SPREAD
    assert {r.open_time_ns: r.reason for r in askr.rejected}[
        t(12, 0)
    ] is RejectReason.NEGATIVE_SPREAD
    flat = {r.open_time_ns: r.status for r in bid.rows}[t(11, 0)]
    assert flat is SlotStatus.FLAT_ZERO_VOLUME


def test_t8_10_import_is_deterministic(server: Server, tmp_path: Path) -> None:
    _serve_days(server, "EURUSD", DAYS)
    cache = RawCache(tmp_path / "raw")
    _dl(server, cache, DAYS)
    ids = set()
    for k in range(2):
        res, raw = import_dukascopy(cache, "EURUSD", BID, DAYS[0], DAYS[-1])
        ref = write_dataset(
            tmp_path / f"ds{k}",
            res,
            symbol="EURUSD",
            basis=BID,
            provenance=DatasetProvenance.REAL,
            coverage_start=datetime(2024, 2, 6, tzinfo=UTC),
            coverage_end=datetime(2024, 2, 9, tzinfo=UTC),
            source={"raw_sha256": raw},
        )
        ids.add(ref.dataset_id)
    assert len(ids) == 1
    assert ids == {REGRESSION_DATASET_ID}


REGRESSION_DATASET_ID = "7858831f1674b3ddf7ac3b93d7224e633c71a8ae0af0d783f362479b1c1a7425"
_ = timedelta, FetchStatus
