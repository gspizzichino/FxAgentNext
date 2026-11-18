"""What the shadow AI reads, point in time.

Only records of the news archive received at or before ``as_of`` are used:
the calendar, the COT report and the central bank texts (decision 1 of the
proposal: no headlines, no retail sentiment). Every source gets an id (S1, S2...)
and keeps the sha256 the archive stored, so a judgement can be traced to the
exact bytes it read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from fxnext.ai.schema import BANK_OF, CURRENCIES, Kind

COT_NAMES = {"099741": "EUR", "096742": "GBP", "097741": "JPY"}
CB_LOOKBACK = timedelta(days=60)
CB_TEXTS_PER_BANK = 2
CB_TEXT_CHARS = 8_000
CALENDAR_BACK = timedelta(days=2)
CALENDAR_AHEAD = timedelta(days=7)


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    received_at: str
    sha256: str
    text: str


@dataclass(frozen=True)
class Trigger:
    """A rate decision text that calls for an event judgement."""

    currency: str
    bank: str
    title: str
    received_at: datetime
    sha256: str


def _records(root: Path, source: str) -> Iterator[dict[str, Any]]:
    folder = root / source
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("http_status") == 200:
                yield rec


def _received(rec: dict[str, Any]) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(rec.get("received_at")))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _known_at(root: Path, source: str, as_of: datetime) -> list[tuple[datetime, dict[str, Any]]]:
    out = []
    for rec in _records(root, source):
        at = _received(rec)
        if at is not None and at <= as_of:
            out.append((at, rec))
    out.sort(key=lambda x: x[0])
    return out


def _latest(root: Path, source: str, as_of: datetime) -> tuple[datetime, dict[str, Any]] | None:
    known = _known_at(root, source, as_of)
    return known[-1] if known else None


def calendar_text(rec: dict[str, Any], as_of: datetime) -> str:
    lines = []
    body = rec.get("body")
    for ev in body if isinstance(body, list) else []:
        if not isinstance(ev, dict) or ev.get("country") not in CURRENCIES:
            continue
        try:
            at = datetime.fromisoformat(str(ev.get("date"))).astimezone(UTC)
        except ValueError:
            continue
        if not as_of - CALENDAR_BACK <= at <= as_of + CALENDAR_AHEAD:
            continue
        parts = [
            f"{at:%Y-%m-%d %H:%M} UTC",
            str(ev.get("country")),
            str(ev.get("impact") or "-"),
            str(ev.get("title") or "-"),
            f"previsione {ev.get('forecast') or '-'}",
            f"precedente {ev.get('previous') or '-'}",
        ]
        if ev.get("actual"):
            parts.append(f"effettivo {ev['actual']}")
        lines.append((at, " | ".join(parts)))
    lines.sort(key=lambda x: x[0])
    return "\n".join(t for _, t in lines) or "(nessun evento per EUR, GBP, JPY, USD)"


def _net(row: dict[str, Any], a: str, b: str) -> str:
    try:
        return f"{float(row[a]) - float(row[b]):+,.0f}"
    except (KeyError, TypeError, ValueError):
        return "-"


def cot_text(rec: dict[str, Any]) -> str:
    rows = [r for r in rec.get("body") or [] if isinstance(r, dict)]
    rows.sort(key=lambda r: str(r.get("report_date_as_yyyy_mm_dd")), reverse=True)
    lines = []
    for r in rows:
        ccy = COT_NAMES.get(str(r.get("cftc_contract_market_code")))
        if ccy is None:
            continue
        lines.append(
            f"{str(r.get('report_date_as_yyyy_mm_dd', ''))[:10]} {ccy}: fondi a leva netti "
            f"{_net(r, 'lev_money_positions_long', 'lev_money_positions_short')}, "
            f"asset manager netti "
            f"{_net(r, 'asset_mgr_positions_long', 'asset_mgr_positions_short')}, "
            f"open interest {r.get('open_interest_all', '-')}"
        )
    return "\n".join(lines) or "(nessuna riga COT)"


def _cb(rec: dict[str, Any]) -> dict[str, Any]:
    body = rec.get("body")
    return body if isinstance(body, dict) else {}


def collect_sources(root: Path, as_of: datetime) -> list[Source]:
    sources: list[Source] = []

    def add(label: str, at: datetime, sha: str, text: str) -> None:
        sources.append(Source(f"S{len(sources) + 1}", label, at.isoformat(), sha, text))

    cal = _latest(root, "calendar", as_of)
    if cal is not None:
        add(
            "Calendario Forex Factory",
            cal[0],
            str(cal[1].get("sha256")),
            calendar_text(cal[1], as_of),
        )
    cot = _latest(root, "cot", as_of)
    if cot is not None:
        add(
            "COT CFTC (Traders in Financial Futures)",
            cot[0],
            str(cot[1].get("sha256")),
            cot_text(cot[1]),
        )
    texts = [
        (at, rec) for at, rec in _known_at(root, "cb-text", as_of) if at >= as_of - CB_LOOKBACK
    ]
    for ccy in CURRENCIES:
        bank = BANK_OF[ccy]
        mine = [(at, rec) for at, rec in texts if _cb(rec).get("bank") == bank]
        for at, rec in mine[-CB_TEXTS_PER_BANK:]:
            cb = _cb(rec)
            label = f"{bank.upper()} ({ccy}): {cb.get('title', '')}"
            if cb.get("published"):
                label += f", pubblicato {cb['published']}"
            add(label, at, str(rec.get("sha256")), str(cb.get("text", ""))[:CB_TEXT_CHARS])
    return sources


def published_at(value: str) -> datetime | None:
    """Publication time from a feed item (RFC 822 or ISO 8601), in UTC."""

    value = value.strip()
    if not value:
        return None
    try:
        at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC)


def pending_triggers(
    root: Path, as_of: datetime, done: set[str], window: timedelta
) -> list[Trigger]:
    """Decision texts *published* in the last ``window`` that have no event run yet.

    The publication time comes from the bank's feed. The time the archive first
    saw a text is not enough: on the first run it downloads decisions weeks old.
    A text without a readable publication time never triggers an event run.
    """

    out = []
    for at, rec in _known_at(root, "cb-text", as_of):
        cb = _cb(rec)
        sha = str(rec.get("sha256"))
        if not cb.get("decision") or sha in done:
            continue
        published = published_at(str(cb.get("published", "")))
        if published is None or not as_of - window <= published <= as_of + timedelta(hours=1):
            continue
        ccy = str(cb.get("currency"))
        if ccy in CURRENCIES:
            out.append(Trigger(ccy, str(cb.get("bank")), str(cb.get("title", "")), at, sha))
    return out


def user_message(
    kind: Kind,
    as_of: datetime,
    currencies: tuple[str, ...],
    sources: list[Source],
    previous: dict[str, dict[str, Any]],
    trigger: Trigger | None = None,
) -> str:
    head = [
        f"Tipo di giudizio: {kind}",
        f"Ora del giudizio: {as_of.astimezone(UTC):%Y-%m-%d %H:%M} UTC",
        f"Valute da giudicare: {', '.join(currencies)}",
    ]
    if trigger is not None:
        sid = next((s.id for s in sources if s.sha256 == trigger.sha256), "?")
        head.append(f"Decisione appena pubblicata: {sid} ({trigger.bank.upper()}, {trigger.title})")
    prev = [
        f"- {c}: tono {v.get('tone')}, bias 5g {v.get('bias_5d')} (del {v.get('as_of')})"
        for c, v in sorted(previous.items())
        if c in currencies
    ]
    if prev:
        head.append("Tuoi giudizi precedenti:\n" + "\n".join(prev))
    body = [f"### {s.id}: {s.label}\nricevuto {s.received_at}\n{s.text}" for s in sources] or [
        "(nessuna fonte disponibile)"
    ]
    return "\n".join(head) + "\n\n## Fonti\n\n" + "\n\n".join(body)


__all__ = [
    "Source",
    "Trigger",
    "calendar_text",
    "collect_sources",
    "cot_text",
    "pending_triggers",
    "user_message",
]
