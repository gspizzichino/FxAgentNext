"""Main loop on the PC: engine step, Telegram commands and alerts, watchdog, read-only check."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from fxnext.domain.calendar import FxCalendar
from fxnext.domain.contracts import EMPTY_STATE, Abstention
from fxnext.domain.enums import Timeframe
from fxnext.domain.timeutil import from_ns
from fxnext.execution.broker import Broker
from fxnext.execution.config import DemoConfig
from fxnext.execution.engine import DemoEngine, trading_day_start_ns
from fxnext.execution.feed import dataset_from_bars, snapshot_for
from fxnext.execution.safety import PIP_SIZE, NotDemoAccount, ServerClock, demo_guard
from fxnext.ops import store as ops
from fxnext.ops import telegram as tg
from fxnext.ops.models import BrokerPosition, OrderRequest, SendResult, SendStatus
from fxnext.strategy.donchian import DonchianBreakout

_NS = 1_000_000_000

HELP = (
    "Comandi: /stop ferma le nuove entrate (kill switch), /stato mostra lo stato. "
    "Per riattivare usa la dashboard."
)


def now_ns() -> int:
    return (time.time_ns() // 1000) * 1000


class ReadOnlyBroker:
    """Wraps a broker and refuses every order: for the read-only phase (dry run)."""

    def __init__(self, inner: Broker) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def send(self, request: OrderRequest) -> SendResult:
        return SendResult(SendStatus.REJECTED, retcode=0, comment="sola lettura: non inviato")

    def close_position(
        self, position: BrokerPosition, deviation_points: int, comment: str
    ) -> SendResult:
        return SendResult(SendStatus.REJECTED, retcode=0, comment="sola lettura: non inviato")


def status_text(store: ops.OpsStore, at_ns: int, engine: DemoEngine | None = None) -> str:
    lines = [f"FxAgent Next demo, {from_ns(at_ns):%d/%m %H:%M} UTC"]
    beat = store.heartbeat("engine")
    if beat is not None:
        lines.append(f"Ultimo giro del motore: {int((at_ns - beat) / _NS)} s fa")
    blockers = engine.entry_blockers(at_ns) if engine is not None else []
    if not blockers:
        for key, label in (
            (ops.KILL_SWITCH, "kill switch"),
            (ops.WEEKLY_BLOCK, "limite sett."),
            (ops.SUSPENDED, "sospeso"),
        ):
            if store.control_on(key):
                blockers.append(label)
    lines.append("Entrate: " + ("FERME (" + ", ".join(blockers) + ")" if blockers else "attive"))
    pos = store.open_positions()
    if pos:
        for p in pos:
            lines.append(f"Aperta {p['symbol']} {p['direction']} {p['volume']} a {p['open_price']}")
    else:
        lines.append("Nessuna posizione aperta")
    lines.append(
        f"R oggi {float(store.get_meta('r_today') or 0):+.2f}, "
        f"settimana {float(store.get_meta('r_week') or 0):+.2f}"
    )
    trades = store.trades()
    if trades:
        total = sum(float(t["r_net"]) for t in trades)
        lines.append(f"Trade chiusi: {len(trades)}, totale {total:+.2f} R")
    return "\n".join(lines)


@dataclass
class TelegramLink:
    config: tg.TelegramConfig
    send: Callable[[tg.TelegramConfig, str], None] = tg.send_message
    fetch: Callable[[tg.TelegramConfig, int | None], list[tg.IncomingCommand]] = tg.get_commands

    def poll(self, store: ops.OpsStore, engine: DemoEngine, at_ns: int) -> None:
        raw = store.get_meta("telegram_offset")
        offset = int(raw) if raw is not None else None
        for cmd in self.fetch(self.config, offset):
            store.set_meta("telegram_offset", str(cmd.update_id + 1))
            if not cmd.command:
                continue  # another chat: ignored
            if cmd.command == "stop":
                store.add_command(ops.CMD_KILL_ON, "telegram", at_ns, "/stop")
                self.send(
                    self.config,
                    "Ricevuto: fermo le nuove entrate (kill switch). "
                    "Le posizioni aperte restano protette da stop e target.",
                )
            elif cmd.command in ("stato", "status"):
                self.send(self.config, status_text(store, at_ns, engine))
            else:
                self.send(self.config, HELP)

    def flush_alerts(self, store: ops.OpsStore) -> None:
        for row in store.unsent_alerts():
            prefix = {"CRITICAL": "🛑 ", "WARN": "⚠️ ", "ERROR": "❗ "}.get(row["level"], "")
            self.send(self.config, prefix + row["text"])
            store.mark_alert_sent(int(row["id"]))


def run_loop(
    engine: DemoEngine,
    store: ops.OpsStore,
    *,
    telegram: TelegramLink | None = None,
    clock: Callable[[], int] = now_ns,
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
    log: Callable[[str], None] = print,
) -> None:
    i = 0
    while max_iterations is None or i < max_iterations:
        i += 1
        t = clock()
        rep = engine.step(t)
        for e in rep.errors:
            log(f"{from_ns(t):%H:%M:%S} errore: {e}")
        for cid in rep.orders:
            log(f"{from_ns(t):%H:%M:%S} ordine {cid}")
        day = str(trading_day_start_ns(t))
        if store.get_meta("summary_day") != day:
            if store.get_meta("summary_day") is not None:
                store.alert(t, "INFO", "Riepilogo giornaliero\n" + status_text(store, t, engine))
            store.set_meta("summary_day", day)
        if telegram is not None:
            try:
                telegram.poll(store, engine, t)
                telegram.flush_alerts(store)
            except tg.TelegramError as exc:
                store.observe(t, "telegram_error", {"error": str(exc)})
                log(f"Telegram: {exc}")
        sleep(engine.config.loop_seconds)


def watchdog(db: Path, max_age_s: float, at_ns: int) -> str | None:
    """Message to send if the engine heartbeat is missing or older than ``max_age_s``."""

    if not db.exists():
        return "Motore demo: database operativo assente, il motore non è mai partito."
    conn = ops.connect(db, readonly=True)
    try:
        row = conn.execute("SELECT at_ns FROM heartbeats WHERE name = 'engine'").fetchone()
    finally:
        conn.close()
    if row is None:
        return "Motore demo: nessun battito registrato."
    age = (at_ns - int(row["at_ns"])) / _NS
    if age > max_age_s:
        return f"Motore demo FERMO: ultimo giro {int(age // 60)} minuti fa."
    return None


def readonly_check(broker: Broker, config: DemoConfig, at_ns: int) -> dict[str, Any]:
    """Phase B: everything the engine would read, no order, no write to MT5."""

    out: dict[str, Any] = {"at": from_ns(at_ns).isoformat()}
    account = broker.account()
    out["account"] = {
        "login": account.login,
        "server": account.server,
        "trade_mode": account.trade_mode.name,
        "currency": account.currency,
        "balance": account.balance,
        "trade_allowed": account.trade_allowed,
    }
    try:
        demo_guard(account, config)
        out["demo_guard"] = "OK"
    except NotDemoAccount as exc:
        out["demo_guard"] = f"BLOCCATO: {exc}"
    offsets = set()
    symbols: dict[str, Any] = {}
    calendar = FxCalendar()
    for sym, bsym in config.symbols.items():
        spec = broker.symbol_spec(bsym)
        tick = broker.tick(bsym)
        info: dict[str, Any] = {
            "broker_symbol": bsym,
            "digits": spec.digits,
            "volume_min": spec.volume_min,
            "volume_step": spec.volume_step,
            "stops_level_points": spec.stops_level_points,
            "freeze_level_points": spec.freeze_level_points,
            "trade_allowed": spec.trade_allowed,
        }
        if tick is not None:
            est = ServerClock.estimate(tick, at_ns)
            offsets.add(est)
            info["spread_pips"] = round((tick.ask - tick.bid) / PIP_SIZE[sym], 2)
            info["offset_s_estimate"] = est
        symbols[sym] = info
    out["symbols"] = symbols
    out["offset_s"] = sorted(offsets)
    positions = broker.positions()
    out["positions"] = {
        "ours": sum(1 for p in positions if p.magic == config.magic),
        "foreign": [
            [p.ticket, p.symbol, p.direction.value, p.volume, p.magic]
            for p in positions
            if p.magic != config.magic
        ],
    }
    if len(offsets) == 1:
        offset = offsets.pop()
        strategy = DonchianBreakout()
        cfg = strategy.default_config()
        req = strategy.requirements(cfg)[0]
        grid = calendar.slot_grid(Timeframe.H1, from_ns(at_ns) - timedelta(days=4), from_ns(at_ns))
        if grid.size:
            signal_open = int(grid[-1])
            as_of = signal_open + Timeframe.H1.ns
            evals = {}
            for sym, bsym in config.symbols.items():
                bars = broker.bars_m15(bsym, config.bars_lookback)
                ds = dataset_from_bars(sym, bars, offset, as_of, calendar)
                if ds is None:
                    evals[sym] = "nessuna candela"
                    continue
                snap = snapshot_for(ds, calendar, req.timeframe, signal_open, req.slots_back, as_of)
                ev = strategy.evaluate(snap, cfg, EMPTY_STATE)
                o = ev.outcome
                evals[sym] = (
                    o.reason_code.value
                    if isinstance(o, Abstention)
                    else f"CANDIDATO {o.direction.value} stop {o.stop:.5f} target {o.target:.5f}"
                )
            out["last_slot"] = from_ns(signal_open).isoformat()
            out["evaluations"] = evals
    return out


def dry_run_config(config: DemoConfig) -> DemoConfig:
    db = config.ops_db
    return replace(config, ops_db=db.with_name(db.stem + "-sola-lettura" + db.suffix))


__all__ = [
    "ReadOnlyBroker",
    "TelegramLink",
    "dry_run_config",
    "now_ns",
    "readonly_check",
    "run_loop",
    "status_text",
    "watchdog",
]
