"""Read-only access to what research and data commands already wrote on disk.

Nothing here writes, launches a run or touches the network. Every function takes
explicit paths and returns plain dicts, so the web layer stays thin and the
readers can be tested on their own.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from fxnext.research.execution import PIP_SIZE
from fxnext.research.protocol import SPLITS

SPLIT_LABELS = {"development": "sviluppo", "validation": "validazione", "holdout": "holdout"}


@dataclass(frozen=True)
class Sources:
    """Where the dashboard looks. All paths are read, never written."""

    research: Path = Path("research")
    prereg: tuple[Path, ...] = (Path("data/prereg"),)
    datasets: Path = Path("data/datasets")
    raw: tuple[Path, ...] = (Path("data/raw"),)
    frozen: tuple[Path, ...] = ()
    news: Path = Path("data/news")
    ops: Path = Path("data/ops/demo.sqlite")
    ai: Path = Path("data/ai")


def _read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_of_day(day: date) -> str:
    t = datetime(day.year, day.month, day.day, tzinfo=UTC)
    for name, (start, end) in SPLITS.items():
        if start <= t < end:
            return name
    return "outside"


# --------------------------------------------------------------------------- experiments


def _experiment_row(result_dir: Path, report: dict[str, Any], group: str) -> dict[str, Any]:
    exec_x1 = (report.get("streams", {}).get("exec") or {}).get("x1") or {}
    recon = report.get("reconciliation") or {}
    balanced = all(bool(v.get("balanced")) for v in recon.values()) if recon else None
    return {
        "fingerprint": report["fingerprint"],
        "short": report["fingerprint"][:8],
        "experiment_id": report["experiment_id"],
        "hypothesis": report.get("hypothesis", ""),
        "split": report["split"],
        "split_label": SPLIT_LABELS.get(report["split"], report["split"]),
        "symbols": sorted((report.get("per_symbol") or {}).keys()),
        "strategy": report.get("strategy") or {},
        "trades": exec_x1.get("trades"),
        "expectancy": exec_x1.get("expectancy"),
        "ci": (exec_x1.get("ci_low"), exec_x1.get("ci_high")),
        "outcome": (report.get("criteria") or {}).get("outcome"),
        "balanced": balanced,
        "provenance": report.get("provenance"),
        "code_version": (report.get("versions") or {}).get("code_version", ""),
        "prereg_sha256": report.get("prereg_sha256", ""),
        "group": group,
        "path": str(result_dir),
        "mtime": result_dir.joinpath("report.json").stat().st_mtime,
    }


def list_experiments(research_root: Path) -> list[dict[str, Any]]:
    """Every ``report.json`` up to one level of grouping (e.g. ``research/check/<fp>``).

    Holdout reports are listed with their identity only: their numbers are not
    returned, so no page can show them by accident.
    """

    rows: list[dict[str, Any]] = []
    if not research_root.is_dir():
        return rows
    candidates: list[tuple[Path, str]] = []
    for child in sorted(research_root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "report.json").is_file():
            candidates.append((child, ""))
        else:
            for sub in sorted(child.iterdir()):
                if sub.is_dir() and (sub / "report.json").is_file():
                    candidates.append((sub, child.name))
    for result_dir, group in candidates:
        try:
            report = _read_json(result_dir / "report.json")
        except (OSError, ValueError, KeyError):
            continue
        row = _experiment_row(result_dir, report, group)
        if row["split"] == "holdout":
            row.update(trades=None, expectancy=None, ci=(None, None), outcome="SEALED")
        rows.append(row)
    rows.sort(key=lambda r: (r["group"] != "", r["experiment_id"], -r["mtime"]))
    return rows


def find_experiment(research_root: Path, fingerprint: str) -> tuple[Path, dict[str, Any]] | None:
    """Locate a result directory by full fingerprint or unique prefix (>= 8 chars)."""

    if len(fingerprint) < 8 or not all(c in "0123456789abcdef" for c in fingerprint):
        return None
    matches = [
        r for r in list_experiments(research_root) if r["fingerprint"].startswith(fingerprint)
    ]
    if len(matches) != 1:
        return None
    result_dir = Path(matches[0]["path"])
    report = _read_json(result_dir / "report.json")
    if report.get("split") == "holdout":
        return None
    return result_dir, report


def equity_curve(result_dir: Path, stream: str = "exec", variant: str = "x1") -> dict[str, Any]:
    """Cumulative R trade by trade, in exit order, for one stream of one experiment."""

    path = result_dir / "outcomes.parquet"
    if not path.is_file():
        return {"points": [], "symbols": []}
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            "SELECT exit_ns, r_net, symbol, reason, direction FROM read_parquet(?) "
            "WHERE stream = ? AND variant = ? AND r_net IS NOT NULL ORDER BY exit_ns, outcome_id",
            [str(path), stream, variant],
        ).fetchall()
    finally:
        con.close()
    points: list[tuple[int, float]] = []
    total = 0.0
    reasons: dict[str, int] = defaultdict(int)
    for exit_ns, r_net, _symbol, reason, _direction in rows:
        total += float(r_net)
        points.append((int(exit_ns), total))
        reasons[str(reason)] += 1
    return {
        "points": points,
        "symbols": sorted({str(r[2]) for r in rows}),
        "reasons": dict(sorted(reasons.items())),
        "trades": len(rows),
    }


def recent_trades(
    result_dir: Path, limit: int = 50, stream: str = "exec", variant: str = "x1"
) -> list[dict[str, Any]]:
    path = result_dir / "outcomes.parquet"
    if not path.is_file():
        return []
    con = duckdb.connect(":memory:")
    try:
        cur = con.execute(
            "SELECT symbol, direction, entry_ns, exit_ns, reason, r_gross, r_net, mae_r, mfe_r "
            "FROM read_parquet(?) WHERE stream = ? AND variant = ? AND r_net IS NOT NULL "
            "ORDER BY exit_ns DESC LIMIT ?",
            [str(path), stream, variant, limit],
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


# --------------------------------------------------------------------------- summaries


def list_summaries(research_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not research_root.is_dir():
        return out
    for path in sorted(research_root.rglob("summary.json")):
        if len(path.relative_to(research_root).parts) > 3:
            continue
        try:
            s = _read_json(path)
        except (OSError, ValueError):
            continue
        if s.get("split") == "holdout":
            continue
        s["_name"] = str(path.parent.relative_to(research_root)).replace("\\", "/")
        s["_depth"] = len(path.relative_to(research_root).parts)
        s["_mtime"] = path.stat().st_mtime
        out.append(s)
    # top-level summaries first (nested ones are checks), newest first
    out.sort(key=lambda x: (x["_depth"], -x["_mtime"]))
    return out


def find_summary(research_root: Path, name: str) -> dict[str, Any] | None:
    for s in list_summaries(research_root):
        if s["_name"] == name:
            return s
    return None


# --------------------------------------------------------------------------- pre-registrations


def list_preregistrations(
    prereg_dirs: tuple[Path, ...], experiments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """TOML pre-registrations with their current sha256, matched to executed reports.

    A report whose ``prereg_sha256`` differs from today's file means the file
    changed after the run: that is flagged, never silently accepted.
    """

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in experiments:
        by_id[e["experiment_id"]].append(e)
    out: list[dict[str, Any]] = []
    for d in prereg_dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.toml")):
            sha = sha256_file(path)
            try:
                doc = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                out.append({"file": path.name, "sha256": sha, "error": str(exc), "status": "ERROR"})
                continue
            exp_id = str(doc.get("experiment_id", path.stem))
            runs = by_id.get(exp_id, [])
            primary = [r for r in runs if not r["group"]]
            if not runs:
                status = "NOT_RUN"
            elif all(r["prereg_sha256"] == sha for r in primary or runs):
                status = "RUN"
            else:
                status = "CHANGED"
            out.append(
                {
                    "file": path.name,
                    "dir": str(d),
                    "experiment_id": exp_id,
                    "hypothesis": doc.get("hypothesis", ""),
                    "split": doc.get("split", ""),
                    "split_label": SPLIT_LABELS.get(
                        str(doc.get("split", "")), doc.get("split", "")
                    ),
                    "symbols": [
                        x.get("symbol") for x in doc.get("data", []) if isinstance(x, dict)
                    ],
                    "sha256": sha,
                    "status": status,
                    "runs": [r["short"] for r in runs],
                }
            )
    return out


def frozen_documents(paths: tuple[Path, ...]) -> list[dict[str, Any]]:
    """Frozen reference documents with a ``<file>.sha256`` next to them."""

    out: list[dict[str, Any]] = []
    for d in paths:
        if not d.is_dir():
            continue
        for sidecar in sorted(d.glob("*.sha256")):
            target = sidecar.with_suffix("")
            expected = (
                sidecar.read_text(encoding="utf-8").split()[0] if sidecar.stat().st_size else ""
            )
            actual = sha256_file(target) if target.is_file() else ""
            out.append(
                {
                    "file": target.name,
                    "expected": expected,
                    "actual": actual,
                    "ok": bool(actual) and actual == expected,
                }
            )
    return out


# --------------------------------------------------------------------------- data coverage


def _iter_fetch_logs(raw_roots: tuple[Path, ...]) -> list[Path]:
    logs: list[Path] = []
    for root in raw_roots:
        if not root.is_dir():
            continue
        if (root / "fetch_log.jsonl").is_file():
            logs.append(root / "fetch_log.jsonl")
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "fetch_log.jsonl").is_file():
                logs.append(child / "fetch_log.jsonl")
    return logs


def data_coverage(raw_roots: tuple[Path, ...]) -> dict[str, Any]:
    """Days per symbol, side and year, from the downloader's append-only fetch logs.

    The last record for a (symbol, side, day) wins, as in the downloader. Only
    weekdays are counted as expected days: weekend files exist but are tiny.
    """

    last: dict[tuple[str, str, str], dict[str, Any]] = {}
    latest_fetch = ""
    for log in _iter_fetch_logs(raw_roots):
        with log.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                key = (rec.get("symbol", ""), rec.get("basis", ""), rec.get("day", ""))
                prev = last.get(key)
                if prev is None or rec.get("fetched_at", "") >= prev.get("fetched_at", ""):
                    last[key] = rec
                latest_fetch = max(latest_fetch, str(rec.get("fetched_at", "")))
    cells: dict[tuple[str, str], dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"ok": 0, "failed": 0})
    )
    for (symbol, basis, day), rec in last.items():
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        bucket = cells[(symbol, basis)][d.year]
        if rec.get("status") == "OK":
            bucket["ok"] += 1
        else:
            bucket["failed"] += 1
    rows: list[dict[str, Any]] = []
    years: set[int] = set()
    for (symbol, basis), by_year in sorted(cells.items()):
        years.update(by_year)
        rows.append({"symbol": symbol, "basis": basis, "years": dict(by_year)})
    return {"rows": rows, "years": sorted(years), "latest_fetch": latest_fetch}


def expected_days(year: int, until: date | None = None) -> int:
    """Calendar days in a year (the downloader fetches every day, weekends included)."""

    start = date(year, 1, 1)
    end = date(year, 12, 31)
    if until is not None and until < end:
        end = until
    return max((end - start).days + 1, 0)


def list_datasets(datasets_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not datasets_root.is_dir():
        return out
    for d in sorted(datasets_root.iterdir()):
        manifest = d / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            m = _read_json(manifest)
        except (OSError, ValueError):
            continue
        q = m.get("quality") or {}
        out.append(
            {
                "dataset_id": m.get("dataset_id", d.name),
                "symbol": m.get("symbol"),
                "basis": m.get("basis"),
                "timeframe": m.get("timeframe"),
                "provenance": m.get("provenance"),
                "rows": m.get("rows"),
                "rejected": m.get("rejected", 0),
                "start": _ns_to_date(m.get("coverage_start_ns")),
                "end": _ns_to_date(m.get("coverage_end_ns")),
                "missing": q.get("in_session_missing"),
            }
        )
    out.sort(key=lambda r: (str(r["symbol"]), str(r["start"]), str(r["basis"])))
    return out


def _ns_to_date(ns: Any) -> str:
    if not isinstance(ns, int):
        return ""
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).date().isoformat()


__all__ = [
    "SPLIT_LABELS",
    "Sources",
    "data_coverage",
    "equity_curve",
    "expected_days",
    "find_experiment",
    "find_summary",
    "frozen_documents",
    "list_datasets",
    "list_experiments",
    "list_preregistrations",
    "list_summaries",
    "recent_trades",
    "sha256_file",
    "split_of_day",
]


# --------------------------------------------------------------------------- demo engine (M3)

_LIMIT_KEYS = ("kill_switch", "suspended", "weekly_block", "daily_block_until_ns", "disconnected")


_ROME = ZoneInfo("Europe/Rome")


def _iso(ns: int | None) -> str:
    """Demo pages show Italian time, like the news page and Telegram."""

    if ns is None:
        return "–"
    return datetime.fromtimestamp(int(ns) / 1e9, tz=UTC).astimezone(_ROME).strftime("%d/%m %H:%M")


LIVE_STALE_S = 60  # older than this, the live prices are shown as not current


def _live(beat: Any, now_ns: int) -> dict[str, Any]:
    """The engine's last snapshot of its positions (heartbeat "protection")."""

    if beat is None:
        return {"positions": [], "at": "–", "age_s": None, "fresh": False}
    try:
        detail = json.loads(beat["detail"] or "{}")
    except ValueError:
        detail = {}
    age = (now_ns - int(beat["at_ns"])) / 1e9
    return {
        "positions": [x for x in detail.get("live") or [] if isinstance(x, dict)],
        "balance": detail.get("balance"),
        "equity": detail.get("equity"),
        "currency": detail.get("currency") or "",
        "at": datetime.fromtimestamp(int(beat["at_ns"]) / 1e9, tz=UTC)
        .astimezone(_ROME)
        .strftime("%H:%M:%S"),
        "age_s": age,
        "fresh": age < LIVE_STALE_S,
    }


def _mark(row: dict[str, Any], snap: dict[str, Any] | None) -> None:
    """Current price, result in account currency and open R of one position."""

    row["price_now"] = row["result"] = row["r_open"] = row["pips_open"] = None
    if snap is None:
        return
    price = float(snap.get("price") or 0.0)
    row["result"] = float(snap.get("profit") or 0.0) + float(snap.get("swap") or 0.0)
    if price <= 0:
        return
    sign = 1.0 if row["direction"] == "LONG" else -1.0
    move = sign * (price - float(row["open_price"]))
    row["price_now"] = price
    row["pips_open"] = move / PIP_SIZE.get(row["symbol"], 1e-4)
    risk = float(row["risk_price"] or 0.0)
    row["r_open"] = move / risk if risk > 0 else None


def demo_view(db: Path, now: datetime) -> dict[str, Any] | None:
    """Everything the demo pages show, read from the engine's SQLite in read-only mode."""

    if not db.exists():
        return None
    from fxnext.ops.store import connect

    now_ns = int(now.timestamp() * 1e9)
    conn = connect(db, readonly=True)
    try:
        q = conn.execute
        meta = {r["key"]: r["value"] for r in q("SELECT key, value FROM meta")}
        controls = {r["key"]: dict(r) for r in q("SELECT * FROM controls")}
        beat_rows = list(q("SELECT name, at_ns, detail FROM heartbeats"))
        beats = {r["name"]: int(r["at_ns"]) for r in beat_rows}
        protection = next((r for r in beat_rows if r["name"] == "protection"), None)
        positions = [dict(r) for r in q("SELECT * FROM positions WHERE status = 'OPEN'")]
        trades = [dict(r) for r in q("SELECT * FROM trades ORDER BY exit_ns DESC LIMIT 200")]
        evaluations = [
            dict(r) for r in q("SELECT * FROM evaluations ORDER BY as_of_ns DESC LIMIT 60")
        ]
        intents = [dict(r) for r in q("SELECT * FROM intents ORDER BY created_ns DESC LIMIT 60")]
        alerts = [dict(r) for r in q("SELECT * FROM alerts ORDER BY id DESC LIMIT 30")]
        commands = [dict(r) for r in q("SELECT * FROM commands ORDER BY id DESC LIMIT 20")]
    finally:
        conn.close()
    blockers = []
    for key in _LIMIT_KEYS:
        c = controls.get(key)
        if c is None:
            continue
        if key == "daily_block_until_ns":
            if int(c["value"]) > now_ns:
                blockers.append(("limite giornaliero", f"fino al {_iso(int(c['value']))}"))
        elif c["value"] == "on":
            label = {
                "kill_switch": "kill switch",
                "suspended": "sospensione",
                "weekly_block": "limite settimanale",
                "disconnected": "MT5 non raggiungibile",
            }[key]
            blockers.append((label, c["reason"]))
    beat = beats.get("engine")
    age = None if beat is None else (now_ns - beat) / 1e9
    live = _live(protection, now_ns)
    by_ticket = {int(x.get("ticket", 0)): x for x in live["positions"]}
    for row in positions:
        row["open_at"] = _iso(row["open_ns"])
        row["time_exit_at"] = _iso(row["time_exit_ns"])
        _mark(row, by_ticket.get(int(row["ticket"])))
    for row in trades:
        row["entry_at"] = _iso(row["entry_ns"])
        row["exit_at"] = _iso(row["exit_ns"])
    for row in evaluations:
        row["at"] = _iso(row["as_of_ns"])
    for row in intents:
        row["at"] = _iso(row["created_ns"])
    for row in alerts:
        row["at"] = _iso(row["at_ns"])
    for row in commands:
        row["at"] = _iso(row["at_ns"])
    total_r = sum(float(t["r_net"]) for t in trades)
    slips = [
        float(t["entry_slippage_pips"]) for t in trades if t["entry_slippage_pips"] is not None
    ]
    return {
        "alive": age is not None and age < 120,
        "age_s": age,
        "account": meta.get("account_binding", "–"),
        "offset_h": None
        if meta.get("server_offset_s") is None
        else int(meta["server_offset_s"]) / 3600,
        "blockers": blockers,
        "kill": controls.get("kill_switch", {}).get("value") == "on",
        "weekly": controls.get("weekly_block", {}).get("value") == "on",
        "suspended": controls.get("suspended", {}).get("value") == "on",
        "r_today": float(meta.get("r_today", 0) or 0),
        "r_week": float(meta.get("r_week", 0) or 0),
        "positions": positions,
        "live": live,
        "open_result": sum(p["result"] for p in positions if p["result"] is not None),
        "open_r": sum(p["r_open"] for p in positions if p["r_open"] is not None),
        "foreign": json.loads(meta.get("foreign_positions", "[]") or "[]"),
        "trades": trades,
        "trade_count": len(trades),
        "total_r": total_r,
        "mean_slippage": (sum(slips) / len(slips)) if slips else None,
        "evaluations": evaluations,
        "intents": intents,
        "alerts": alerts,
        "commands": commands,
    }
