"""Official statements of the Fed, ECB, BoE and BoJ, archived point in time.

Each run reads the four press feeds (``cb-feed-<bank>``) and, for every new item that
looks like a monetary policy text, downloads the page once and keeps its plain
text (``cb-text``). Both go through :func:`fxnext.news.archive.store`, so every
record carries the UTC time it was received and the sha256 of what was read.

Only the banks' own hosts are contacted. From the cloud these sites answer 403:
the archive runs on the PC, like the rest of the news archive.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from fxnext.news.archive import Fetcher, Snapshot, SourceError, store


@dataclass(frozen=True)
class Bank:
    code: str
    currency: str
    name: str
    feed: str
    hosts: tuple[str, ...]
    # an item is kept when its title contains one of these (lower case)
    keep: tuple[str, ...]
    # ... and it is a rate decision when the title contains one of these
    decision: tuple[str, ...]


BANKS: dict[str, Bank] = {
    "fed": Bank(
        "fed",
        "USD",
        "Federal Reserve",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        ("www.federalreserve.gov",),
        ("fomc statement", "minutes of the federal open market committee", "monetary policy"),
        ("fomc statement",),
    ),
    "ecb": Bank(
        "ecb",
        "EUR",
        "BCE",
        "https://www.ecb.europa.eu/rss/press.html",
        ("www.ecb.europa.eu",),
        ("monetary policy decision", "monetary policy statement", "account of the monetary"),
        ("monetary policy decision",),
    ),
    "boe": Bank(
        "boe",
        "GBP",
        "Bank of England",
        "https://www.bankofengland.co.uk/rss/news",
        ("www.bankofengland.co.uk",),
        ("bank rate", "monetary policy summary", "monetary policy report"),
        ("bank rate", "monetary policy summary"),
    ),
    "boj": Bank(
        "boj",
        "JPY",
        "Bank of Japan",
        "https://www.boj.or.jp/en/rss/whatsnew.xml",
        ("www.boj.or.jp",),
        ("statement on monetary policy", "summary of opinions", "outlook for economic activity"),
        ("statement on monetary policy",),
    ),
}
MAX_TEXT_CHARS = 60_000


@dataclass(frozen=True)
class FeedItem:
    bank: str
    title: str
    url: str
    published: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(bank: Bank, body: bytes) -> list[FeedItem]:
    """RSS 2.0 or Atom items. Unknown formats give an empty list (the raw feed is kept)."""

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items: list[FeedItem] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in node:
            name = _local(child.tag)
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href", ""))
            elif child.text:
                fields.setdefault(name, child.text.strip())
        title, link = fields.get("title", ""), fields.get("link", "")
        if title and link:
            published = fields.get("pubdate") or fields.get("updated") or fields.get("date", "")
            items.append(FeedItem(bank.code, " ".join(title.split()), link.strip(), published))
    return items


def is_relevant(bank: Bank, title: str) -> bool:
    low = title.lower()
    return any(k in low for k in bank.keep)


def is_decision(bank: Bank, title: str) -> bool:
    low = title.lower()
    return any(k in low for k in bank.decision)


class _Text(HTMLParser):
    SKIP = frozenset({"script", "style", "nav", "header", "footer", "noscript", "svg", "form"})
    BLOCK = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def html_to_text(body: bytes) -> str:
    parser = _Text()
    parser.feed(body.decode("utf-8", errors="replace"))
    text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:MAX_TEXT_CHARS]


def _known_urls(root: Path) -> set[str]:
    folder = root / "cb-text"
    seen: set[str] = set()
    if not folder.is_dir():
        return seen
    for path in folder.glob("*/*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("http_status") == 200:
                seen.add(str(rec.get("url")))
    return seen


def archive_banks(
    root: Path, get: Fetcher, now: Callable[[], datetime], banks: tuple[str, ...] = tuple(BANKS)
) -> dict[str, str]:
    """Store each feed, then the text of new relevant items. Returns an outcome per bank."""

    known = _known_urls(root)
    out: dict[str, str] = {}
    for code in banks:
        bank = BANKS[code]
        try:
            status, body = get(bank.feed)
            store(root, Snapshot(f"cb-feed-{code}", bank.feed, status, body), now())
            if status != 200:
                out[code] = f"error: HTTP {status} sul feed"
                continue
            new = 0
            for item in parse_feed(bank, body):
                host = urllib.parse.urlparse(item.url).hostname or ""
                if item.url in known or host not in bank.hosts:
                    continue
                if not is_relevant(bank, item.title):
                    continue
                status, page = get(item.url)
                record: dict[str, Any] = {
                    "bank": code,
                    "currency": bank.currency,
                    "title": item.title,
                    "published": item.published,
                    "decision": is_decision(bank, item.title),
                    "text": html_to_text(page) if status == 200 else "",
                }
                payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
                store(root, Snapshot("cb-text", item.url, status, payload), now())
                if status == 200:
                    known.add(item.url)
                    new += 1
            out[code] = f"saved {new} new" if new else "unchanged"
        except SourceError as exc:
            out[code] = f"error: {exc}"
    return out


__all__ = [
    "BANKS",
    "Bank",
    "FeedItem",
    "archive_banks",
    "html_to_text",
    "is_decision",
    "is_relevant",
    "parse_feed",
]
