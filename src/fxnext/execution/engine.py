"""Demo engine: one ``step`` per loop iteration (M0 spec §7, M3 plan).

Order of a step, each part isolated so that a failure in one does not skip the
others:

1. commands (kill switch, resume) from the queue;
2. account: demo guard and binding. If the account is not the allowlisted demo
   account the step stops here and touches nothing, not even our positions;
3. server clock from fresh ticks;
4. reconciliation: broker positions and deals are the truth;
5. protection: SL/TP present, time exit after ``time_exit_slots`` H1 slots;
6. loss limits (closed trades, in R);
7. signals: evaluate each symbol once per closed H1 slot with the research
   strategy, then intent -> preflight -> send.

Entries happen only in step 7 and only if every gate in ``entry_blockers`` is clear.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np

from fxnext.domain.calendar import FxCalendar, ny_1700_utc
from fxnext.domain.contracts import EMPTY_STATE, Abstention, Candidate
from fxnext.domain.enums import Direction, ReasonCode, Timeframe
from fxnext.domain.timeutil import from_ns, to_ns
from fxnext.execution.broker import DRY_RUN_COMMENT, Broker, BrokerUnavailable
from fxnext.execution.config import DemoConfig
from fxnext.execution.feed import dataset_from_bars, snapshot_for
from fxnext.execution.safety import (
    PIP_SIZE,
    NotDemoAccount,
    ServerClock,
    demo_guard,
    preflight,
)
from fxnext.ops import store as ops
from fxnext.ops.models import (
    FILLED_STATES,
    AccountIdentity,
    BrokerDeal,
    BrokerPosition,
    IntentState,
    OrderRequest,
    SendResult,
    SendStatus,
)
from fxnext.strategy.donchian import DonchianBreakout

_NS = 1_000_000_000
_H1 = Timeframe.H1.ns
# Abstentions caused by missing candles: retried for ``data_retry_s`` before recording.
_DATA_REASONS = frozenset(
    {ReasonCode.BAR_NOT_AVAILABLE, ReasonCode.DATA_GAP, ReasonCode.INSUFFICIENT_HISTORY}
)

CrashHook = Callable[[str], None]


def client_order_id(candidate_id: str) -> str:
    """Short id for the MT5 comment (<= 31 chars), unique per candidate."""

    return "fxn-" + candidate_id[:24]


def trading_day_start_ns(now_ns: int) -> int:
    """Start of the NY trading day (17:00 New York) containing ``now_ns``."""

    now = from_ns(now_ns)
    for d in (now.date() + timedelta(days=1), now.date(), now.date() - timedelta(days=1)):
        start = ny_1700_utc(d - timedelta(days=1))
        if to_ns(start) <= now_ns < to_ns(ny_1700_utc(d)):
            return to_ns(start)
    raise AssertionError("unreachable")


def trading_week_start_ns(now_ns: int) -> int:
    """Sunday 17:00 New York opening the trading week of ``now_ns``."""

    d = from_ns(now_ns).date()
    for _ in range(9):
        if d.weekday() == 6 and to_ns(ny_1700_utc(d)) <= now_ns:
            return to_ns(ny_1700_utc(d))
        d -= timedelta(days=1)
    raise AssertionError("unreachable")


def trading_day_end_ns(now_ns: int) -> int:
    start = from_ns(trading_day_start_ns(now_ns))
    return to_ns(ny_1700_utc((start + timedelta(days=1)).date()))


@dataclass
class StepReport:
    at_ns: int
    guard_ok: bool = False
    evaluations: int = 0
    orders: list[str] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DemoEngine:
    def __init__(
        self,
        broker: Broker,
        store: ops.OpsStore,
        config: DemoConfig,
        *,
        calendar: FxCalendar | None = None,
        crash_hook: CrashHook = lambda point: None,
    ) -> None:
        self.broker = broker
        self.store = store
        self.config = config
        self.calendar = calendar or FxCalendar()
        self.clock = ServerClock()
        saved = store.get_meta("server_offset_s")
        if saved is not None:
            self.clock.offset_s = int(saved)
        self.strategy = DonchianBreakout()
        self.strategy_config = self.strategy.default_config()
        self.req = self.strategy.requirements(self.strategy_config)[0]
        self.crash_hook = crash_hook
        self.account: AccountIdentity | None = None
        self._startup_done = False
        self._missing_close: dict[int, int] = {}

    # ================================================================== public
    def step(self, now_ns: int) -> StepReport:
        rep = StepReport(now_ns)
        if not self._startup_done:
            self._startup(now_ns)
        self._apply_commands(now_ns)
        try:
            account = self.broker.account()
            demo_guard(account, self.config)
            self._bind(account, now_ns)
            self.account = account
            rep.guard_ok = True
        except NotDemoAccount as exc:
            self._suspend(now_ns, f"conto non demo o non autorizzato: {exc}", level="CRITICAL")
            rep.errors.append(str(exc))
            self.store.beat("engine", now_ns, {"guard": "FAILED"})
            return rep
        except BrokerUnavailable as exc:
            rep.errors.append(f"broker: {exc}")
            self._note_disconnect(now_ns, str(exc))
            self.store.beat("engine", now_ns, {"broker": "unavailable"})
            return rep
        self._reconnected(now_ns)
        for name, part in (
            ("clock", self._update_clock),
            ("reconcile", self._reconcile),
            ("protect", self._protect),
            ("limits", self._limits),
            ("signals", self._signals),
        ):
            try:
                part(now_ns, rep)
            except BrokerUnavailable as exc:
                rep.errors.append(f"{name}: broker {exc}")
                self._note_disconnect(now_ns, str(exc))
                break
            except ops.OpsIntegrityError:
                raise
            except Exception as exc:  # noqa: BLE001 - one part must not stop the others
                rep.errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self.store.observe(now_ns, "error", {"part": name, "error": repr(exc)})
                self._alert(now_ns, "ERROR", f"Errore nel motore ({name}): {exc}")
        self.store.beat(
            "engine",
            now_ns,
            {"blockers": rep.blockers, "errors": rep.errors, "offset_s": self.clock.offset_s},
        )
        return rep

    def entry_blockers(self, now_ns: int, symbol: str | None = None) -> list[str]:
        s = self.store
        out = []
        if s.control_on(ops.KILL_SWITCH):
            out.append("kill switch")
        if s.control_on(ops.SUSPENDED):
            row = s.control(ops.SUSPENDED)
            out.append(f"sospeso: {row['reason'] if row else ''}")
        if s.control_on(ops.WEEKLY_BLOCK):
            out.append("limite settimanale")
        daily = s.control(ops.DAILY_BLOCK_UNTIL)
        if daily is not None and int(daily["value"]) > now_ns:
            out.append("limite giornaliero")
        if self.clock.offset_s is None:
            out.append("orario del server non rilevato")
        if s.control_on("disconnected"):
            out.append("broker disconnesso")
        unknown = s.intents_in({IntentState.UNKNOWN, IntentState.SUBMITTING})
        if symbol is not None and any(r["symbol"] == symbol for r in unknown):
            out.append(f"ordine con esito ignoto su {symbol}")
        return out

    # ================================================================== startup
    def _startup(self, now_ns: int) -> None:
        with self.store.tx():
            for row in self.store.intents_in({IntentState.SUBMITTING, IntentState.PENDING}):
                to = (
                    IntentState.UNKNOWN
                    if row["state"] == IntentState.SUBMITTING
                    else IntentState.BLOCKED
                )
                self.store.transition(row["client_order_id"], to, now_ns, {"why": "restart"})
        self._startup_done = True
        self.store.observe(now_ns, "startup", {"magic": self.config.magic})

    # ================================================================== commands
    def _apply_commands(self, now_ns: int) -> None:
        for cmd in self.store.pending_commands():
            name, source = cmd["command"], cmd["source"]
            if name == ops.CMD_KILL_ON:
                self.store.set_control(ops.KILL_SWITCH, "on", f"comando da {source}", now_ns)
                result = "kill switch attivo"
                self._alert(now_ns, "WARN", f"Kill switch ATTIVO ({source}): nuove entrate ferme.")
            elif name == ops.CMD_KILL_OFF:
                if source == "telegram":
                    result = "rifiutato: si riattiva solo dalla dashboard"
                else:
                    self.store.set_control(ops.KILL_SWITCH, "off", f"comando da {source}", now_ns)
                    result = "kill switch disattivato"
                    self._alert(now_ns, "INFO", f"Kill switch disattivato ({source}).")
            elif name == ops.CMD_RESUME_WEEKLY:
                if source == "telegram":
                    result = "rifiutato: si riattiva solo dalla dashboard"
                else:
                    self.store.set_control(ops.WEEKLY_BLOCK, "off", f"ripresa da {source}", now_ns)
                    result = "limite settimanale azzerato"
                    self._alert(now_ns, "INFO", f"Entrate riattivate dopo il limite ({source}).")
            elif name == ops.CMD_CLEAR_SUSPENSION:
                if source == "telegram":
                    result = "rifiutato: si riattiva solo dalla dashboard"
                else:
                    self.store.set_control(ops.SUSPENDED, "off", f"tolta da {source}", now_ns)
                    result = "sospensione tolta"
                    self._alert(now_ns, "INFO", f"Sospensione tolta ({source}).")
            else:
                result = "comando sconosciuto"
            self.store.command_done(int(cmd["id"]), now_ns, result)

    # ================================================================== account
    def _bind(self, account: AccountIdentity, now_ns: int) -> None:
        key = f"{account.server}:{account.login}"
        bound = self.store.get_meta("account_binding")
        if bound is None:
            self.store.set_meta("account_binding", key)
            self.store.observe(now_ns, "account_bound", {"account": key})
        elif bound != key:
            raise NotDemoAccount(f"account changed from {bound} to {key}")

    def _suspend(self, now_ns: int, reason: str, level: str = "WARN") -> None:
        if not self.store.control_on(ops.SUSPENDED):
            self.store.set_control(ops.SUSPENDED, "on", reason, now_ns)
            self._alert(now_ns, level, f"Entrate SOSPESE: {reason}")

    def _note_disconnect(self, now_ns: int, detail: str) -> None:
        if not self.store.control_on("disconnected"):
            self.store.set_control("disconnected", "on", detail, now_ns)
            self._alert(now_ns, "WARN", f"MT5 non raggiungibile: {detail}. Entrate ferme.")

    def _reconnected(self, now_ns: int) -> None:
        if self.store.control_on("disconnected"):
            self.store.set_control("disconnected", "off", "riconnesso", now_ns)
            self._alert(now_ns, "INFO", "MT5 di nuovo raggiungibile.")

    def _alert(self, now_ns: int, level: str, text: str) -> None:
        self.store.alert(now_ns, level, text)

    # ================================================================== clock
    def _update_clock(self, now_ns: int, rep: StepReport) -> None:
        if not self.calendar.is_in_session(from_ns(now_ns)):
            return
        estimates = set()
        for broker_symbol in self.config.symbols.values():
            tick = self.broker.tick(broker_symbol)
            if tick is None:
                continue
            est = ServerClock.estimate(tick, now_ns)
            # a tick older than a few minutes says nothing about the offset
            if self.clock.offset_s is not None:
                age = (now_ns - self.clock.to_utc_ns(tick.server_ns)) / _NS
                if abs(age) > self.config.max_tick_age_s:
                    continue
            estimates.add(est)
        if len(estimates) != 1:
            return
        est = estimates.pop()
        if self.clock.offset_s is None:
            self.clock.offset_s = est
            self.store.set_meta("server_offset_s", str(est))
            self.store.observe(now_ns, "server_offset", {"offset_s": est})
            self._alert(now_ns, "INFO", f"Orario server rilevato: UTC{est / 3600:+.2f} h")
        elif est != self.clock.offset_s:
            self._suspend(
                now_ns,
                f"orario del server cambiato da {self.clock.offset_s}s a {est}s: verificare",
            )
            self.store.observe(
                now_ns, "server_offset_changed", {"old": self.clock.offset_s, "new": est}
            )

    # ================================================================== reconciliation
    def _research_symbol(self, broker_symbol: str) -> str | None:
        for k, v in self.config.symbols.items():
            if v == broker_symbol:
                return k
        return None

    def _reconcile(self, now_ns: int, rep: StepReport) -> None:
        if self.clock.offset_s is None:
            return
        positions = self.broker.positions()
        ours = {p.ticket: p for p in positions if p.magic == self.config.magic}
        foreign = [p for p in positions if p.magic != self.config.magic]
        self.store.beat(
            "reconcile",
            now_ns,
            {"ours": len(ours), "foreign": len(foreign)},
        )
        lo = self.clock.to_server_s(now_ns) - 14 * 86_400
        hi = self.clock.to_server_s(now_ns) + 3600
        deals: list[BrokerDeal] | None = None

        def get_deals() -> list[BrokerDeal]:
            nonlocal deals
            if deals is None:
                deals = [d for d in self.broker.deals(lo, hi) if d.magic == self.config.magic]
            return deals

        with self.store.tx():
            # 1. intents with unknown outcome
            for row in self.store.intents_in({IntentState.UNKNOWN}):
                cid = row["client_order_id"]
                pos = next((p for p in ours.values() if p.comment == cid), None)
                deal = next((d for d in get_deals() if d.entry == "IN" and d.comment == cid), None)
                if pos is not None or deal is not None:
                    ticket = pos.ticket if pos is not None else deal.position_id  # type: ignore[union-attr]
                    price = pos.open_price if pos is not None else deal.price  # type: ignore[union-attr]
                    vol = pos.volume if pos is not None else deal.volume  # type: ignore[union-attr]
                    self.store.transition(
                        cid,
                        IntentState.RECONCILED_FILLED,
                        now_ns,
                        {"ticket": ticket},
                        position_ticket=ticket,
                        fill_price=price,
                        fill_volume=vol,
                    )
                    self._track_position(row, ticket, price, vol, pos, now_ns)
                    self._alert(
                        now_ns,
                        "INFO",
                        f"Ordine {row['symbol']} con esito ignoto: "
                        f"risulta ESEGUITO (posizione {ticket}).",
                    )
                elif now_ns - int(row["updated_ns"]) > self.config.reconcile_tolerance_s * _NS:
                    self.store.transition(
                        cid, IntentState.RECONCILED_ABSENT, now_ns, {"why": "not found at broker"}
                    )
                    self._alert(
                        now_ns,
                        "INFO",
                        f"Ordine {row['symbol']} con esito ignoto: "
                        "NON eseguito. Non viene reinviato.",
                    )
            # 2. our broker positions unknown to the DB -> adopt and protect
            known = {int(r["ticket"]) for r in self.store.open_positions()}
            for ticket, pos in ours.items():
                if ticket in known:
                    continue
                if self.store.position(ticket) is not None:
                    continue
                intent = self.store.intent(pos.comment) if pos.comment else None
                if intent is not None:
                    self._track_position(intent, ticket, pos.open_price, pos.volume, pos, now_ns)
                    continue
                symbol = self._research_symbol(pos.symbol) or pos.symbol
                self.store.upsert_position(
                    {
                        "ticket": ticket,
                        "client_order_id": None,
                        "symbol": symbol,
                        "broker_symbol": pos.symbol,
                        "direction": pos.direction.value,
                        "volume": pos.volume,
                        "open_price": pos.open_price,
                        "open_ns": self.clock.to_utc_ns(pos.open_server_ns),
                        "sl": pos.sl,
                        "tp": pos.tp,
                        "risk_price": abs(pos.open_price - pos.sl) if pos.sl else 0.0,
                        "time_exit_ns": self._time_exit_ns(
                            self.clock.to_utc_ns(pos.open_server_ns)
                        ),
                        "status": "OPEN",
                        "adopted": 1,
                    }
                )
                self._alert(
                    now_ns,
                    "WARN",
                    f"Posizione {ticket} {pos.symbol} con il nostro magic "
                    "non era nel database: adottata.",
                )
                rep.blockers.append(f"adottata posizione {ticket}")
            # 3. DB positions no longer at the broker -> closed
            for row in self.store.open_positions():
                ticket = int(row["ticket"])
                if ticket in ours:
                    self._missing_close.pop(ticket, None)
                    continue
                outs = [d for d in get_deals() if d.position_id == ticket and d.entry == "OUT"]
                if not outs:
                    first = self._missing_close.setdefault(ticket, now_ns)
                    if now_ns - first > self.config.reconcile_tolerance_s * _NS:
                        self._suspend(now_ns, f"posizione {ticket} sparita senza deal di chiusura")
                    continue
                self._missing_close.pop(ticket, None)
                self._record_close(row, outs, get_deals(), now_ns)
                rep.closed.append(ticket)
        if foreign:
            self.store.set_meta(
                "foreign_positions",
                json.dumps(
                    [[p.ticket, p.symbol, p.direction.value, p.volume, p.magic] for p in foreign]
                ),
            )
        else:
            self.store.set_meta("foreign_positions", "[]")

    def _time_exit_ns(self, entry_ns: int) -> int:
        grid = self.calendar.slot_grid(
            Timeframe.H1,
            from_ns(entry_ns) - timedelta(hours=2),
            from_ns(entry_ns) + timedelta(days=30),
        )
        idx = int(np.searchsorted(grid, entry_ns, side="right")) - 1
        last = max(idx, 0) + self.config.time_exit_slots - 1
        return int(grid[last]) + _H1

    def _track_position(
        self,
        intent: Any,
        ticket: int,
        price: float,
        volume: float,
        pos: BrokerPosition | None,
        now_ns: int,
    ) -> None:
        open_ns = self.clock.to_utc_ns(pos.open_server_ns) if pos is not None else now_ns
        self.store.upsert_position(
            {
                "ticket": ticket,
                "client_order_id": intent["client_order_id"],
                "symbol": intent["symbol"],
                "broker_symbol": intent["broker_symbol"],
                "direction": intent["direction"],
                "volume": volume,
                "open_price": price,
                "open_ns": open_ns,
                "sl": pos.sl if pos is not None else (intent["sl_sent"] or intent["stop"]),
                "tp": pos.tp if pos is not None else (intent["tp_sent"] or intent["target"]),
                "risk_price": abs(float(intent["entry_ref"]) - float(intent["stop"])),
                "time_exit_ns": self._time_exit_ns(open_ns),
                "status": "OPEN",
                "adopted": 0,
            }
        )

    def _record_close(
        self, row: Any, outs: list[BrokerDeal], all_deals: list[BrokerDeal], now_ns: int
    ) -> None:
        ticket = int(row["ticket"])
        ins = [d for d in all_deals if d.position_id == ticket and d.entry == "IN"]
        direction = Direction(row["direction"])
        sign = 1.0 if direction is Direction.LONG else -1.0
        vol = sum(d.volume for d in outs)
        exit_price = sum(d.price * d.volume for d in outs) / vol if vol else outs[-1].price
        profit = sum(d.profit for d in outs)
        swap = sum(d.swap for d in outs + ins)
        commission = sum(d.commission for d in outs + ins)
        entry_price = float(row["open_price"])
        sl = float(row["sl"]) if row["sl"] else 0.0
        risk_money = 0.0
        if sl:
            r = self.broker.calc_profit(
                row["broker_symbol"], direction, float(row["volume"]), entry_price, sl
            )
            risk_money = abs(r) if r else 0.0
        r_net = (profit + swap + commission) / risk_money if risk_money > 0 else 0.0
        reason = outs[-1].reason or "OTHER"
        pip = PIP_SIZE.get(row["symbol"], 1e-4)
        intent = self.store.intent(row["client_order_id"]) if row["client_order_id"] else None
        entry_slip = exit_slip = spread = None
        if intent is not None and intent["ref_ask"] is not None:
            ref = intent["ref_ask"] if direction is Direction.LONG else intent["ref_bid"]
            entry_slip = sign * (entry_price - float(ref)) / pip  # >0: paid more than quoted
            spread = (float(intent["ref_ask"]) - float(intent["ref_bid"])) / pip
        if reason in ("SL", "TP"):
            level = float(row["sl"] if reason == "SL" else row["tp"])
            exit_slip = sign * (level - exit_price) / pip  # >0: worse than the level
        self.store.record_trade(
            {
                "position_ticket": ticket,
                "client_order_id": row["client_order_id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "volume": float(row["volume"]),
                "entry_ref": None if intent is None else float(intent["entry_ref"]),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "entry_ns": int(row["open_ns"]),
                "exit_ns": self.clock.to_utc_ns(outs[-1].server_ns),
                "exit_reason": reason,
                "profit": profit,
                "swap": swap,
                "commission": commission,
                "risk_money": risk_money,
                "r_net": r_net,
                "entry_slippage_pips": entry_slip,
                "exit_slippage_pips": exit_slip,
                "spread_entry_pips": spread,
            }
        )
        self.store.close_position(ticket)
        self._alert(
            now_ns,
            "INFO",
            f"Chiusa {row['symbol']} {row['direction']} ({reason}): "
            f"{profit + swap + commission:+.2f} {self.config.account_currency}, {r_net:+.2f} R",
        )

    # ================================================================== protection
    def _protect(self, now_ns: int, rep: StepReport) -> None:
        if self.clock.offset_s is None:
            return
        broker_pos = {p.ticket: p for p in self.broker.positions() if p.magic == self.config.magic}
        for row in self.store.open_positions():
            ticket = int(row["ticket"])
            pos = broker_pos.get(ticket)
            if pos is None:
                continue
            if not pos.sl or not pos.tp:
                self._suspend(now_ns, f"posizione {ticket} senza stop o target al broker")
            exit_ns = row["time_exit_ns"]
            if exit_ns is not None and now_ns >= int(exit_ns):
                if not self.calendar.is_in_session(from_ns(now_ns)):
                    continue
                account = self.broker.account()
                demo_guard(account, self.config)  # before every close as well
                res = self.broker.close_position(pos, self.config.deviation_points, "fxn-time-exit")
                self.store.observe(
                    now_ns,
                    "time_exit",
                    {"ticket": ticket, "status": res.status.value, "retcode": res.retcode},
                )
                if res.status is not SendStatus.FILLED:
                    self._alert(
                        now_ns,
                        "WARN",
                        f"Uscita a tempo di {ticket} non riuscita "
                        f"({res.retcode} {res.comment}); riprovo al prossimo giro.",
                    )
        self.store.beat("protection", now_ns, {"positions": len(broker_pos)})

    # ================================================================== limits
    def _limits(self, now_ns: int, rep: StepReport) -> None:
        day_start = trading_day_start_ns(now_ns)
        week_start = trading_week_start_ns(now_ns)
        day_r = self.store.r_closed_between(day_start, now_ns + 1)
        week_r = self.store.r_closed_between(week_start, now_ns + 1)
        self.store.set_meta("r_today", f"{day_r:.4f}")
        self.store.set_meta("r_week", f"{week_r:.4f}")
        next_day = trading_day_end_ns(now_ns)
        if day_r <= self.config.daily_limit_r:
            current = self.store.control(ops.DAILY_BLOCK_UNTIL)
            if current is None or int(current["value"]) < next_day:
                self.store.set_control(
                    ops.DAILY_BLOCK_UNTIL, str(next_day), f"{day_r:.2f} R oggi", now_ns
                )
                self._alert(
                    now_ns,
                    "WARN",
                    f"Limite giornaliero raggiunto ({day_r:.2f} R): "
                    "nuove entrate ferme fino al prossimo giorno di trading.",
                )
        if week_r <= self.config.weekly_limit_r:
            marker = f"week:{week_start}"
            if self.store.get_meta("weekly_block_week") != marker:
                self.store.set_meta("weekly_block_week", marker)
                self.store.set_control(
                    ops.WEEKLY_BLOCK, "on", f"{week_r:.2f} R in settimana", now_ns
                )
                self._alert(
                    now_ns,
                    "WARN",
                    f"Limite settimanale raggiunto ({week_r:.2f} R): "
                    "entrate ferme finché non le riattivi dalla dashboard.",
                )

    # ================================================================== signals
    def _due_slot(self, now_ns: int) -> tuple[int, int] | None:
        """(signal slot open, as_of) of the last H1 slot whose as_of has passed."""

        delay = int(self.config.evaluation_delay_s * _NS)
        grid = self.calendar.slot_grid(
            Timeframe.H1, from_ns(now_ns) - timedelta(days=4), from_ns(now_ns)
        )
        for open_ns in grid[::-1].tolist():
            as_of = int(open_ns) + _H1 + delay
            if as_of <= now_ns:
                return int(open_ns), as_of
        return None

    def _signals(self, now_ns: int, rep: StepReport) -> None:
        if self.clock.offset_s is None:
            rep.blockers.append("orario del server non rilevato")
            return
        due = self._due_slot(now_ns)
        if due is None:
            return
        signal_open, as_of = due
        late = now_ns - as_of > self.config.max_lateness_s * _NS
        for symbol, broker_symbol in sorted(self.config.symbols.items()):
            marker = f"slot:{symbol}:{signal_open}"
            if self.store.get_meta("last_slot_" + symbol) == marker:
                continue
            bars = self.broker.bars_m15(broker_symbol, self.config.bars_lookback)
            dataset = dataset_from_bars(symbol, bars, self.clock.offset_s, as_of, self.calendar)
            if dataset is None:
                rep.errors.append(f"{symbol}: nessuna candela")
                continue
            snap = snapshot_for(
                dataset, self.calendar, self.req.timeframe, signal_open, self.req.slots_back, as_of
            )
            ev = self.strategy.evaluate(snap, self.strategy_config, EMPTY_STATE)
            outcome = ev.outcome
            if (
                isinstance(outcome, Abstention)
                and outcome.reason_code in _DATA_REASONS
                and now_ns - as_of < self.config.data_retry_s * _NS
            ):
                # MT5 may still be syncing history (seen on the PC right after
                # symbol_select): read the candles again on the next loop instead of
                # recording a data abstention that would close the slot.
                rep.blockers.append(f"{symbol}: candele incomplete, riprovo")
                continue
            rep.evaluations += 1
            dispatch = "NOT_DISPATCHED_LATE" if late else "DISPATCHED"
            with self.store.tx():
                self.store.record_evaluation(
                    {
                        "request_id": ev.request.request_id,
                        "symbol": symbol,
                        "as_of_ns": as_of,
                        "executed_ns": now_ns,
                        "dispatch": dispatch,
                        "outcome": outcome.reason_code.value
                        if isinstance(outcome, Abstention)
                        else f"CANDIDATE_{outcome.direction.value}",
                        "evaluation_hash": ev.evaluation_hash,
                        "candidate_id": None
                        if isinstance(outcome, Abstention)
                        else outcome.candidate_id,
                        "snapshot_hash": ev.snapshot_hash,
                    }
                )
                self.store.set_meta("last_slot_" + symbol, marker)
            if isinstance(outcome, Candidate) and not late:
                cid = self._enter(
                    symbol, broker_symbol, ev.request.request_id, outcome, now_ns, rep
                )
                if cid:
                    rep.orders.append(cid)

    def _enter(
        self,
        symbol: str,
        broker_symbol: str,
        request_id: str,
        cand: Candidate,
        now_ns: int,
        rep: StepReport,
    ) -> str | None:
        assert self.account is not None
        cid = client_order_id(cand.candidate_id)
        if self.store.intent_for_candidate(cand.candidate_id) is not None:
            return None  # already handled: never twice for the same event
        account_key = f"{self.account.server}:{self.account.login}"
        base = {
            "client_order_id": cid,
            "candidate_id": cand.candidate_id,
            "event_id": cand.event_id,
            "request_id": request_id,
            "account": account_key,
            "symbol": symbol,
            "broker_symbol": broker_symbol,
            "direction": cand.direction.value,
            "volume": self.config.volume,
            "entry_ref": cand.entry_ref,
            "stop": cand.stop,
            "target": cand.target,
            "signal_close_ns": to_ns(cand.available_at),
            "expires_ns": to_ns(cand.expires_at),
            "state": IntentState.PENDING.value,
            "created_ns": now_ns,
            "updated_ns": now_ns,
        }
        blockers = self.entry_blockers(now_ns, symbol)
        ours = [r for r in self.store.open_positions()]
        if any(r["symbol"] == symbol for r in ours):
            blockers.append(f"posizione già aperta su {symbol}")
        if len(ours) >= self.config.max_positions_total:
            blockers.append("numero massimo di posizioni")
        with self.store.tx():
            self.store.insert_intent(base)
            if blockers:
                self.store.transition(cid, IntentState.BLOCKED, now_ns, {"blockers": blockers})
                rep.blockers.extend(blockers)
                return None
        spec = self.broker.symbol_spec(broker_symbol)
        tick = self.broker.tick(broker_symbol)
        tick_utc = None if tick is None else self.clock.to_utc_ns(tick.server_ns)
        account = self.broker.account()
        demo_guard(account, self.config)  # again, right before sending
        pf = preflight(
            symbol=symbol,
            direction=cand.direction,
            stop=cand.stop,
            target=cand.target,
            volume=self.config.volume,
            expires_ns=to_ns(cand.expires_at),
            now_ns=now_ns,
            tick=tick,
            tick_utc_ns=tick_utc,
            spec=spec,
            account=account,
            config=self.config,
            calendar=self.calendar,
        )
        if not pf.ok:
            with self.store.tx():
                self.store.transition(cid, IntentState.BLOCKED, now_ns, {"preflight": pf.reason})
            rep.blockers.append(f"{symbol}: {pf.reason}")
            self._alert(
                now_ns,
                "INFO",
                f"Segnale {symbol} {cand.direction.value} non eseguito: {pf.reason}.",
            )
            return None
        assert tick is not None
        request = OrderRequest(
            client_order_id=cid,
            symbol=broker_symbol,
            direction=cand.direction,
            volume=self.config.volume,
            sl=pf.sl,
            tp=pf.tp,
            magic=self.config.magic,
            reference_price=pf.reference_price,
            deviation_points=self.config.deviation_points,
        )
        with self.store.tx():
            self.store.transition(
                cid,
                IntentState.SUBMITTING,
                now_ns,
                {"sl": pf.sl, "tp": pf.tp, "spread_pips": pf.spread_pips},
                sl_sent=pf.sl,
                tp_sent=pf.tp,
                ref_bid=tick.bid,
                ref_ask=tick.ask,
                sent_ns=now_ns,
            )
        self.crash_hook("after_submitting")
        try:
            result = self.broker.send(request)
        except BrokerUnavailable as exc:
            result = SendResult(SendStatus.UNKNOWN, comment=f"no answer: {exc}")
        self.crash_hook("after_send")
        self._apply_send_result(cid, symbol, cand, result, now_ns)
        return cid

    def _apply_send_result(
        self, cid: str, symbol: str, cand: Candidate, result: SendResult, now_ns: int
    ) -> None:
        state = {
            SendStatus.FILLED: IntentState.FILLED,
            SendStatus.PARTIAL: IntentState.PARTIAL,
            SendStatus.REJECTED: IntentState.REJECTED,
            SendStatus.UNKNOWN: IntentState.UNKNOWN,
        }[result.status]
        with self.store.tx():
            self.store.transition(
                cid,
                state,
                now_ns,
                {"retcode": result.retcode, "comment": result.comment},
                retcode=result.retcode,
                broker_comment=result.comment,
                position_ticket=result.position_ticket,
                fill_price=result.price,
                fill_volume=result.volume,
                answered_ns=now_ns,
            )
            if state in FILLED_STATES and result.position_ticket is not None:
                intent = self.store.intent(cid)
                pos = next(
                    (p for p in self.broker.positions() if p.ticket == result.position_ticket),
                    None,
                )
                self._track_position(
                    intent,
                    result.position_ticket,
                    float(result.price or cand.entry_ref),
                    float(result.volume or self.config.volume),
                    pos,
                    now_ns,
                )
        if state in FILLED_STATES:
            self._alert(
                now_ns,
                "INFO",
                f"Aperta {symbol} {cand.direction.value} "
                f"{result.volume} lotti a {result.price} (SL {cand.stop:.5f}, "
                f"TP {cand.target:.5f}).",
            )
        elif state is IntentState.REJECTED and result.comment == DRY_RUN_COMMENT:
            self._alert(
                now_ns,
                "INFO",
                f"Sola lettura: il motore avrebbe aperto {symbol} {cand.direction.value} "
                f"{self.config.volume} lotti a circa {cand.entry_ref} (SL {cand.stop:.5f}, "
                f"TP {cand.target:.5f}). Nessun ordine inviato al broker.",
            )
        elif state is IntentState.REJECTED:
            self._alert(
                now_ns,
                "WARN",
                f"Ordine {symbol} RIFIUTATO dal broker: {result.retcode} {result.comment}.",
            )
        else:
            self._alert(
                now_ns,
                "WARN",
                f"Ordine {symbol}: esito IGNOTO. Entrate su {symbol} "
                "ferme finché la riconciliazione non decide; nessun reinvio.",
            )


__all__ = [
    "DemoEngine",
    "StepReport",
    "client_order_id",
    "trading_day_start_ns",
    "trading_week_start_ns",
]
