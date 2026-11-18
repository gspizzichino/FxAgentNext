"""Operational state in SQLite WAL (ADR-0004, M0 spec §7).

The engine is the only writer of everything except the ``commands`` table, which
is an append-only queue: the dashboard, Telegram and the CLI add a row, the engine
applies it and records the result. Every intent transition is appended to
``intent_events``, never updated in place.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fxnext.ops.models import IntentState

SCHEMA_VERSION = "ops-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents (
    client_order_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    broker_symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    entry_ref REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    sl_sent REAL,
    tp_sent REAL,
    ref_bid REAL,
    ref_ask REAL,
    signal_close_ns INTEGER NOT NULL,
    expires_ns INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL,
    retcode INTEGER,
    broker_comment TEXT,
    position_ticket INTEGER,
    fill_price REAL,
    fill_volume REAL,
    sent_ns INTEGER,
    answered_ns INTEGER,
    UNIQUE (event_id, account)
);
CREATE TABLE IF NOT EXISTS intent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    at_ns INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    ticket INTEGER PRIMARY KEY,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    broker_symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    open_price REAL NOT NULL,
    open_ns INTEGER NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    risk_price REAL NOT NULL,
    time_exit_ns INTEGER,
    status TEXT NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    position_ticket INTEGER PRIMARY KEY,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    entry_ref REAL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    entry_ns INTEGER NOT NULL,
    exit_ns INTEGER NOT NULL,
    exit_reason TEXT NOT NULL,
    profit REAL NOT NULL,
    swap REAL NOT NULL,
    commission REAL NOT NULL,
    risk_money REAL NOT NULL,
    r_net REAL NOT NULL,
    entry_slippage_pips REAL,
    exit_slippage_pips REAL,
    spread_entry_pips REAL
);
CREATE TABLE IF NOT EXISTS evaluations (
    request_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    as_of_ns INTEGER NOT NULL,
    executed_ns INTEGER NOT NULL,
    dispatch TEXT NOT NULL,
    outcome TEXT NOT NULL,
    evaluation_hash TEXT,
    candidate_id TEXT,
    snapshot_hash TEXT
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ns INTEGER NOT NULL,
    source TEXT NOT NULL,
    command TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    applied_ns INTEGER,
    result TEXT
);
CREATE TABLE IF NOT EXISTS controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    reason TEXT NOT NULL,
    at_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeats (
    name TEXT PRIMARY KEY,
    at_ns INTEGER NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ns INTEGER NOT NULL,
    level TEXT NOT NULL,
    text TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ns INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

# Engine controls (controls.key)
KILL_SWITCH = "kill_switch"  # "on" blocks new entries
DAILY_BLOCK_UNTIL = "daily_block_until_ns"  # entries blocked until this instant
WEEKLY_BLOCK = "weekly_block"  # "on" until a manual RESUME_WEEKLY
SUSPENDED = "suspended"  # "on" with a structured reason (discrepancy, account...)

# Commands accepted from outside the engine.
CMD_KILL_ON = "KILL_ON"
CMD_KILL_OFF = "KILL_OFF"
CMD_RESUME_WEEKLY = "RESUME_WEEKLY"
CMD_CLEAR_SUSPENSION = "CLEAR_SUSPENSION"
COMMANDS = frozenset({CMD_KILL_ON, CMD_KILL_OFF, CMD_RESUME_WEEKLY, CMD_CLEAR_SUSPENSION})


class OpsIntegrityError(RuntimeError):
    pass


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    return conn


def enqueue_command(path: Path, command: str, source: str, at_ns: int, note: str = "") -> int:
    """The only write allowed outside the engine: append a command to the queue."""

    if command not in COMMANDS:
        raise ValueError(f"unknown command {command}")
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        cur = conn.execute(
            "INSERT INTO commands (at_ns, source, command, note) VALUES (?, ?, ?, ?)",
            (at_ns, source, command, note),
        )
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


@dataclass
class OpsStore:
    path: Path

    def __post_init__(self) -> None:
        self.conn = connect(self.path)
        self.conn.executescript(_SCHEMA)
        existing = self.get_meta("schema_version")
        if existing is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        elif existing != SCHEMA_VERSION:
            raise OpsIntegrityError(f"ops db schema {existing}, expected {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ meta / controls
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def control(self, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM controls WHERE key = ?", (key,)
        ).fetchone()
        return row

    def control_on(self, key: str) -> bool:
        row = self.control(key)
        return row is not None and row["value"] == "on"

    def set_control(self, key: str, value: str, reason: str, at_ns: int) -> None:
        self.conn.execute(
            "INSERT INTO controls (key, value, reason, at_ns) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, reason = excluded.reason, "
            "at_ns = excluded.at_ns",
            (key, value, reason, at_ns),
        )
        self.observe(at_ns, "control", {"key": key, "value": value, "reason": reason})

    # ------------------------------------------------------------------ heartbeats / logs
    def beat(self, name: str, at_ns: int, detail: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO heartbeats (name, at_ns, detail) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET at_ns = excluded.at_ns, detail = excluded.detail",
            (name, at_ns, json.dumps(detail or {}, sort_keys=True)),
        )

    def heartbeat(self, name: str) -> int | None:
        row = self.conn.execute("SELECT at_ns FROM heartbeats WHERE name = ?", (name,)).fetchone()
        return None if row is None else int(row["at_ns"])

    def observe(self, at_ns: int, kind: str, detail: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO observations (at_ns, kind, detail) VALUES (?, ?, ?)",
            (at_ns, kind, json.dumps(detail, sort_keys=True, default=str)),
        )

    def alert(self, at_ns: int, level: str, text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO alerts (at_ns, level, text) VALUES (?, ?, ?)", (at_ns, level, text)
        )
        return int(cur.lastrowid or 0)

    def unsent_alerts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM alerts WHERE sent = 0 ORDER BY id"))

    def mark_alert_sent(self, alert_id: int) -> None:
        self.conn.execute("UPDATE alerts SET sent = 1 WHERE id = ?", (alert_id,))

    # ------------------------------------------------------------------ commands
    def pending_commands(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM commands WHERE applied_ns IS NULL ORDER BY id")
        )

    def command_done(self, command_id: int, at_ns: int, result: str) -> None:
        self.conn.execute(
            "UPDATE commands SET applied_ns = ?, result = ? WHERE id = ?",
            (at_ns, result, command_id),
        )

    def add_command(self, command: str, source: str, at_ns: int, note: str = "") -> int:
        if command not in COMMANDS:
            raise ValueError(f"unknown command {command}")
        cur = self.conn.execute(
            "INSERT INTO commands (at_ns, source, command, note) VALUES (?, ?, ?, ?)",
            (at_ns, source, command, note),
        )
        return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------ evaluations
    def evaluation_seen(self, request_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM evaluations WHERE request_id = ?", (request_id,)
        ).fetchone()
        return row is not None

    def record_evaluation(self, row: dict[str, Any]) -> None:
        existing = self.conn.execute(
            "SELECT evaluation_hash FROM evaluations WHERE request_id = ?", (row["request_id"],)
        ).fetchone()
        if existing is not None:
            if existing["evaluation_hash"] != row.get("evaluation_hash"):
                raise OpsIntegrityError("same request_id with a different evaluation")
            return
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO evaluations ({cols}) VALUES ({marks})", tuple(row.values()))

    # ------------------------------------------------------------------ intents
    def insert_intent(self, row: dict[str, Any]) -> None:
        clash = self.conn.execute(
            "SELECT candidate_id FROM intents WHERE client_order_id = ?",
            (row["client_order_id"],),
        ).fetchone()
        if clash is not None:
            if clash["candidate_id"] != row["candidate_id"]:
                raise OpsIntegrityError("client_order_id collision between candidates")
            return
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO intents ({cols}) VALUES ({marks})", tuple(row.values()))
        self._event(row["client_order_id"], row["created_ns"], None, row["state"], {})

    def intent(self, client_order_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM intents WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return row

    def intent_for_candidate(self, candidate_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM intents WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        return row

    def intents_in(self, states: frozenset[IntentState] | set[IntentState]) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in states)
        return list(
            self.conn.execute(
                f"SELECT * FROM intents WHERE state IN ({marks}) ORDER BY created_ns",
                tuple(s.value for s in states),
            )
        )

    def transition(
        self,
        client_order_id: str,
        to: IntentState,
        at_ns: int,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        row = self.intent(client_order_id)
        if row is None:
            raise OpsIntegrityError(f"unknown intent {client_order_id}")
        sets = ", ".join(f"{k} = ?" for k in ("state", "updated_ns", *fields))
        self.conn.execute(
            f"UPDATE intents SET {sets} WHERE client_order_id = ?",
            (to.value, at_ns, *fields.values(), client_order_id),
        )
        self._event(client_order_id, at_ns, row["state"], to.value, detail or {})

    def _event(
        self, cid: str, at_ns: int, frm: str | None, to: str, detail: dict[str, Any]
    ) -> None:
        self.conn.execute(
            "INSERT INTO intent_events (client_order_id, at_ns, from_state, to_state, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, at_ns, frm, to, json.dumps(detail, sort_keys=True, default=str)),
        )

    # ------------------------------------------------------------------ positions / trades
    def upsert_position(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "ticket")
        self.conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(ticket) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    def open_positions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM positions WHERE status = 'OPEN'"))

    def position(self, ticket: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM positions WHERE ticket = ?", (ticket,)
        ).fetchone()
        return row

    def close_position(self, ticket: int) -> None:
        self.conn.execute("UPDATE positions SET status = 'CLOSED' WHERE ticket = ?", (ticket,))

    def record_trade(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT OR IGNORE INTO trades ({cols}) VALUES ({marks})", tuple(row.values())
        )

    def r_closed_between(self, start_ns: int, end_ns: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(r_net), 0.0) AS r FROM trades WHERE exit_ns >= ? AND exit_ns < ?",
            (start_ns, end_ns),
        ).fetchone()
        return float(row["r"])

    def trades(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM trades ORDER BY exit_ns"))


__all__ = [
    "CMD_CLEAR_SUSPENSION",
    "CMD_KILL_OFF",
    "CMD_KILL_ON",
    "CMD_RESUME_WEEKLY",
    "COMMANDS",
    "DAILY_BLOCK_UNTIL",
    "KILL_SWITCH",
    "OpsIntegrityError",
    "OpsStore",
    "SCHEMA_VERSION",
    "SUSPENDED",
    "WEEKLY_BLOCK",
    "connect",
    "enqueue_command",
]
