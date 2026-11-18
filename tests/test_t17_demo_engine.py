"""T17: demo engine against the simulated broker (M3, failure injection of M0 spec §8)."""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fxnext.domain.calendar import FxCalendar
from fxnext.domain.enums import Direction, Timeframe
from fxnext.domain.timeutil import to_ns
from fxnext.execution.config import DemoConfig
from fxnext.execution.engine import DemoEngine, trading_day_start_ns, trading_week_start_ns
from fxnext.execution.feed import dataset_from_bars
from fxnext.execution.mt5 import MT5Broker
from fxnext.execution.runner import (
    ReadOnlyBroker,
    TelegramLink,
    readonly_check,
    run_loop,
    status_text,
    watchdog,
)
from fxnext.execution.safety import (
    NotDemoAccount,
    demo_guard,
    in_rollover_window,
    round_inward,
)
from fxnext.execution.simulated import SimulatedBroker
from fxnext.ops import store as ops
from fxnext.ops import telegram as tg
from fxnext.ops.models import (
    AccountIdentity,
    AccountTradeMode,
    BrokerPosition,
    IntentState,
    RawBar,
    SendStatus,
)

LOGIN = 1234567
SIGNAL_OPEN = datetime(2026, 11, 18, 14, tzinfo=UTC)  # a Wednesday
SIGNAL_CLOSE = SIGNAL_OPEN + timedelta(hours=1)
NOW = SIGNAL_CLOSE + timedelta(seconds=15)
_S = 1_000_000_000


def cfg(tmp_path: Path, **kw: Any) -> DemoConfig:
    base: dict[str, Any] = {
        "allowed_logins": (LOGIN,),
        "symbols": {"EURUSD": "EURUSD^"},
        "max_spread_pips": {"EURUSD": 2.0},
        "ops_db": tmp_path / "ops.sqlite",
    }
    base.update(kw)
    return DemoConfig(**base)


def bars(breakout: str | None = "up", offset_s: int = 0, extra_hours: int = 0) -> list[RawBar]:
    """M15 candles on the calendar grid; a fresh breakout in the 14:00 UTC H1 slot."""

    cal = FxCalendar()
    end = SIGNAL_CLOSE + timedelta(hours=extra_hours)
    grid = cal.slot_grid(Timeframe.M15, SIGNAL_OPEN - timedelta(days=6), end)
    out = []
    sig_ns, close_ns = to_ns(SIGNAL_OPEN), to_ns(SIGNAL_CLOSE)
    for i, t in enumerate(grid.tolist()):
        o = 1.1000 + (0.0002 if i % 2 else -0.0002)
        c = 1.1000 + (-0.0001 if i % 2 else 0.0001)
        h, lo = max(o, c) + 0.0003, min(o, c) - 0.0003
        if sig_ns <= t < close_ns and breakout:
            k = (t - sig_ns) // (15 * 60 * _S)
            step = 0.0008 if breakout == "up" else -0.0008
            o = 1.1000 + step * k
            c = 1.1000 + step * (k + 1)
            h, lo = max(o, c) + 0.0001, min(o, c) - 0.0001
        out.append(RawBar(t // _S + offset_s, o, h, lo, c, 50.0))
    return out


def make(
    tmp_path: Path,
    breakout: str | None = "up",
    offset_s: int = 0,
    **kw: Any,
) -> tuple[SimulatedBroker, ops.OpsStore, DemoEngine]:
    broker = SimulatedBroker(server_offset_s=offset_s)
    broker.bars["EURUSD^"] = bars(breakout, offset_s)
    price = 1.1032 if breakout == "up" else 1.0968 if breakout == "down" else 1.1000
    broker.set_tick("EURUSD^", to_ns(NOW) - 2 * _S, price, price + 0.00008)
    config = cfg(tmp_path, **kw)
    store = ops.OpsStore(config.ops_db)
    return broker, store, DemoEngine(broker, store, config)


def intents(store: ops.OpsStore) -> list[Any]:
    return list(store.conn.execute("SELECT * FROM intents ORDER BY created_ns"))


# ---------------------------------------------------------------- happy path and idempotence


def test_breakout_sends_one_order_with_inward_levels(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    rep = engine.step(to_ns(NOW))
    assert rep.guard_ok and not rep.errors, rep
    assert len(broker.sent) == 1
    req = broker.sent[0]
    assert req.direction is Direction.LONG
    assert req.volume == 0.01 and req.magic == engine.config.magic != 260828
    assert req.client_order_id.startswith("fxn-") and len(req.client_order_id) <= 31
    row = intents(store)[0]
    assert row["state"] == IntentState.FILLED
    assert req.sl >= row["stop"] and req.tp <= row["target"]  # rounded only inward
    assert len(store.open_positions()) == 1
    # same slot again: nothing new, even after a restart
    engine.step(to_ns(NOW) + 5 * _S)
    DemoEngine(broker, store, engine.config).step(to_ns(NOW) + 10 * _S)
    assert len(broker.sent) == 1
    assert len(intents(store)) == 1


def test_short_breakout_and_server_offset(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path, breakout="down", offset_s=7200)
    engine.step(to_ns(NOW))
    assert engine.clock.offset_s == 7200
    assert [r.direction for r in broker.sent] == [Direction.SHORT]


def test_no_breakout_no_order(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path, breakout=None)
    engine.step(to_ns(NOW))
    assert broker.sent == []
    ev = store.conn.execute("SELECT * FROM evaluations").fetchone()
    assert ev["outcome"] == "NO_EVENT"


def test_late_dispatch_records_but_does_not_trade(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    late = to_ns(NOW) + 20 * 60 * _S
    broker.set_tick("EURUSD^", late - _S, 1.1032, 1.10328)
    engine.step(late)
    assert broker.sent == []
    ev = store.conn.execute("SELECT * FROM evaluations").fetchone()
    assert ev["dispatch"] == "NOT_DISPATCHED_LATE"


# ---------------------------------------------------------------- demo guard


@pytest.mark.parametrize(
    "change",
    [
        {"trade_mode": AccountTradeMode.REAL},
        {"trade_mode": AccountTradeMode.CONTEST},
        {"server": "NagaMarkets-Live"},
        {"login": 999},
        {"currency": "USD"},
    ],
)
def test_guard_blocks_anything_but_the_allowlisted_demo(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    broker, store, engine = make(tmp_path)
    a = broker.account_identity
    broker.account_identity = AccountIdentity(
        login=change.get("login", a.login),
        server=change.get("server", a.server),
        trade_mode=change.get("trade_mode", a.trade_mode),
        currency=change.get("currency", a.currency),
    )
    rep = engine.step(to_ns(NOW))
    assert not rep.guard_ok
    assert broker.sent == []
    assert store.control_on(ops.SUSPENDED)
    assert any(r["level"] == "CRITICAL" for r in store.unsent_alerts())


def test_guard_also_blocks_time_exit_closes_on_a_real_account(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    engine.step(to_ns(NOW))
    assert len(broker.open_positions) == 1
    a = broker.account_identity
    broker.account_identity = AccountIdentity(a.login, a.server, AccountTradeMode.REAL, a.currency)
    far = to_ns(NOW) + 30 * 86400 * _S
    engine.step(far)
    assert len(broker.open_positions) == 1  # untouched


def test_account_switch_suspends(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path, allowed_logins=(LOGIN, 7654321))
    early = to_ns(NOW) - 3600 * _S
    broker.set_tick("EURUSD^", early - _S, 1.1, 1.10008)
    engine.step(early)
    assert broker.sent == []
    broker.set_tick("EURUSD^", to_ns(NOW) - _S, 1.1032, 1.10328)
    a = broker.account_identity
    broker.account_identity = AccountIdentity(7654321, a.server, a.trade_mode, a.currency)
    engine.step(to_ns(NOW))
    assert broker.sent == []
    assert store.control_on(ops.SUSPENDED)


def test_demo_guard_unit() -> None:
    config = DemoConfig(allowed_logins=(1,))
    ok = AccountIdentity(1, "NagaMarkets-Demo", AccountTradeMode.DEMO, "EUR")
    demo_guard(ok, config)
    with pytest.raises(NotDemoAccount):
        demo_guard(AccountIdentity(1, "NagaMarkets-Demo", AccountTradeMode.REAL, "EUR"), config)


def test_config_rejects_legacy_magic_and_big_size(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cfg(tmp_path, magic=260828)
    with pytest.raises(ValueError):
        cfg(tmp_path, volume=0.5, max_volume=0.5)
    with pytest.raises(ValueError):
        DemoConfig(allowed_logins=())


# ---------------------------------------------------------------- unknown outcome, crash


def test_lost_response_is_unknown_never_resent_then_reconciled(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    broker.lose_next_response = True
    engine.step(to_ns(NOW))
    assert intents(store)[0]["state"] == IntentState.UNKNOWN
    assert "ordine con esito ignoto su EURUSD" in engine.entry_blockers(to_ns(NOW), "EURUSD")
    engine.step(to_ns(NOW) + 5 * _S)
    assert len(broker.sent) == 1  # never resent
    row = intents(store)[0]
    assert row["state"] == IntentState.RECONCILED_FILLED
    assert len(store.open_positions()) == 1


def test_failure_before_execution_reconciles_absent(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    broker.raise_next_send = True
    engine.step(to_ns(NOW))
    assert intents(store)[0]["state"] == IntentState.UNKNOWN
    engine.step(to_ns(NOW) + 30 * _S)
    assert intents(store)[0]["state"] == IntentState.UNKNOWN  # within tolerance
    engine.step(to_ns(NOW) + 200 * _S)
    assert intents(store)[0]["state"] == IntentState.RECONCILED_ABSENT
    assert len(broker.sent) == 1
    assert broker.open_positions == {}


def test_crash_between_persist_and_send(tmp_path: Path) -> None:
    broker, store, _ = make(tmp_path)

    def crash(point: str) -> None:
        if point == "after_submitting":
            raise SystemExit("killed")

    engine = DemoEngine(broker, store, cfg(tmp_path), crash_hook=crash)
    with pytest.raises(SystemExit):
        engine.step(to_ns(NOW))
    assert intents(store)[0]["state"] == IntentState.SUBMITTING
    assert broker.sent == []
    restarted = DemoEngine(broker, store, cfg(tmp_path))
    restarted.step(to_ns(NOW) + 5 * _S)
    assert intents(store)[0]["state"] == IntentState.UNKNOWN
    restarted.step(to_ns(NOW) + 300 * _S)
    assert intents(store)[0]["state"] == IntentState.RECONCILED_ABSENT
    assert broker.sent == []


def test_rejected_order(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    broker.reject_next = 10016
    engine.step(to_ns(NOW))
    row = intents(store)[0]
    assert row["state"] == IntentState.REJECTED and row["retcode"] == 10016
    assert store.open_positions() == []


def test_partial_fill_tracked(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path, volume=0.02, max_volume=0.02)
    broker.partial_next = 0.5
    engine.step(to_ns(NOW))
    assert intents(store)[0]["state"] == IntentState.PARTIAL
    assert store.open_positions()[0]["volume"] == pytest.approx(0.01)


# ---------------------------------------------------------------- disconnect, orphans


def test_disconnect_blocks_and_recovers(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    broker.connected = False
    rep = engine.step(to_ns(NOW))
    assert rep.errors and broker.sent == []
    assert "broker disconnesso" in engine.entry_blockers(to_ns(NOW))
    broker.connected = True
    engine.step(to_ns(NOW) + 5 * _S)
    assert "broker disconnesso" not in engine.entry_blockers(to_ns(NOW) + 5 * _S)


def test_orphan_with_our_magic_is_adopted_foreign_is_untouched(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path, breakout=None)
    srv = to_ns(NOW) - 3600 * _S
    broker.add_foreign_position(
        BrokerPosition(5, "EURUSD^", Direction.LONG, 0.01, 1.1, srv, 1.09, 1.12, 260828, "old")
    )
    broker.add_foreign_position(
        BrokerPosition(
            6, "EURUSD^", Direction.SHORT, 0.01, 1.1, srv, 1.11, 1.08, engine.config.magic, "manual"
        )
    )
    engine.step(to_ns(NOW))
    pos = {int(r["ticket"]): r for r in store.open_positions()}
    assert set(pos) == {6} and pos[6]["adopted"] == 1
    assert json.loads(store.get_meta("foreign_positions") or "[]")[0][0] == 5
    engine.step(to_ns(NOW) + 200 * 3600 * _S)  # far past the time exit of the adopted one
    assert 5 in broker.open_positions  # never touched
    assert 6 not in broker.open_positions


def test_position_without_stop_suspends(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    engine.step(to_ns(NOW))
    ticket = next(iter(broker.open_positions))
    p = broker.open_positions[ticket]
    broker.open_positions[ticket] = BrokerPosition(
        p.ticket,
        p.symbol,
        p.direction,
        p.volume,
        p.open_price,
        p.open_server_ns,
        0.0,
        p.tp,
        p.magic,
        p.comment,
    )
    engine.step(to_ns(NOW) + 5 * _S)
    assert store.control_on(ops.SUSPENDED)


# ---------------------------------------------------------------- closes, R, limits


def test_stop_hit_records_trade_in_r(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    engine.step(to_ns(NOW))
    pos = next(iter(broker.open_positions.values()))
    t = to_ns(NOW) + 600 * _S
    broker.set_tick("EURUSD^", t, pos.sl - 0.0001, pos.sl - 0.00002)
    engine.step(t)
    trades = store.trades()
    assert len(trades) == 1 and trades[0]["exit_reason"] == "SL"
    assert -1.2 < trades[0]["r_net"] < -0.8
    assert store.open_positions() == []


def test_time_exit_closes_after_120_slots(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    engine.step(to_ns(NOW))
    row = store.open_positions()[0]
    exit_ns = int(row["time_exit_ns"])
    grid = FxCalendar().slot_grid(Timeframe.H1, SIGNAL_CLOSE, SIGNAL_CLOSE + timedelta(days=14))
    assert exit_ns == int(grid[119]) + Timeframe.H1.ns
    broker.set_tick("EURUSD^", exit_ns + _S, 1.1032, 1.10328)
    engine.step(exit_ns + 2 * _S)
    engine.step(exit_ns + 7 * _S)
    assert broker.open_positions == {}
    assert store.trades()[0]["exit_reason"] == "EXPERT"


def _fake_losses(store: ops.OpsStore, n: int, at_ns: int) -> None:
    for i in range(n):
        store.record_trade(
            {
                "position_ticket": 90000 + i,
                "client_order_id": None,
                "symbol": "EURUSD",
                "direction": "LONG",
                "volume": 0.01,
                "entry_ref": 1.1,
                "entry_price": 1.1,
                "exit_price": 1.099,
                "entry_ns": at_ns - 3600 * _S,
                "exit_ns": at_ns - (i + 1) * _S,
                "exit_reason": "SL",
                "profit": -1.0,
                "swap": 0.0,
                "commission": 0.0,
                "risk_money": 1.0,
                "r_net": -1.0,
            }
        )


def test_daily_limit_blocks_until_next_trading_day(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    _fake_losses(store, 5, to_ns(NOW))
    engine.step(to_ns(NOW))
    assert broker.sent == []
    assert "limite giornaliero" in engine.entry_blockers(to_ns(NOW))
    end = int(store.control(ops.DAILY_BLOCK_UNTIL)["value"])  # type: ignore[index]
    assert end == to_ns(datetime(2026, 11, 18, 22, tzinfo=UTC))  # 17:00 New York
    assert "limite giornaliero" not in engine.entry_blockers(end + _S)


def test_weekly_limit_needs_dashboard_resume(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    t0 = to_ns(NOW) - 2 * 86400 * _S  # Monday, same trading week
    _fake_losses(store, 10, t0)
    engine.step(to_ns(NOW))
    assert broker.sent == []
    assert "limite settimanale" in engine.entry_blockers(to_ns(NOW))
    store.add_command(ops.CMD_RESUME_WEEKLY, "telegram", to_ns(NOW))
    engine.step(to_ns(NOW) + _S)
    assert store.control_on(ops.WEEKLY_BLOCK)  # Telegram cannot resume
    store.add_command(ops.CMD_RESUME_WEEKLY, "dashboard", to_ns(NOW))
    engine.step(to_ns(NOW) + 2 * _S)
    assert not store.control_on(ops.WEEKLY_BLOCK)
    engine.step(to_ns(NOW) + 3 * _S)
    assert not store.control_on(ops.WEEKLY_BLOCK)  # not re-armed in the same week


def test_trading_day_and_week_bounds() -> None:
    wed = to_ns(datetime(2026, 11, 18, 23, tzinfo=UTC))  # after 17:00 NY
    assert trading_day_start_ns(wed) == to_ns(datetime(2026, 11, 18, 22, tzinfo=UTC))
    assert trading_week_start_ns(wed) == to_ns(datetime(2026, 11, 15, 22, tzinfo=UTC))


# ---------------------------------------------------------------- kill switch, preflight


def test_kill_switch_blocks_entries_and_telegram_cannot_lift_it(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    store.add_command(ops.CMD_KILL_ON, "telegram", to_ns(NOW))
    store.add_command(ops.CMD_KILL_OFF, "telegram", to_ns(NOW))
    engine.step(to_ns(NOW))
    assert store.control_on(ops.KILL_SWITCH)
    assert broker.sent == []
    assert intents(store)[0]["state"] == IntentState.BLOCKED
    done = list(store.conn.execute("SELECT command, result FROM commands ORDER BY id"))
    assert "rifiutato" in done[1]["result"]


def test_kill_switch_keeps_protection(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    engine.step(to_ns(NOW))
    store.add_command(ops.CMD_KILL_ON, "dashboard", to_ns(NOW))
    pos = next(iter(broker.open_positions.values()))
    engine.step(to_ns(NOW) + 5 * _S)
    assert pos.ticket in broker.open_positions and pos.sl > 0  # nothing closed by the kill


def test_wide_spread_blocks_order(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    broker.set_tick("EURUSD^", to_ns(NOW) - _S, 1.1032, 1.1062)
    engine.step(to_ns(NOW))
    assert broker.sent == []
    row = intents(store)[0]
    assert row["state"] == IntentState.BLOCKED


def test_stale_tick_blocks_order(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    engine.clock.offset_s = 0
    broker.set_tick("EURUSD^", to_ns(NOW) - 600 * _S, 1.1032, 1.10328)
    engine.step(to_ns(NOW))
    assert broker.sent == []


def test_read_only_broker_never_sends(tmp_path: Path) -> None:
    broker, store, _ = make(tmp_path)
    engine = DemoEngine(ReadOnlyBroker(broker), store, cfg(tmp_path))  # type: ignore[arg-type]
    engine.step(to_ns(NOW))
    assert broker.sent == [] and broker.open_positions == {}
    assert intents(store)[0]["state"] == IntentState.REJECTED


def test_rounding_and_rollover() -> None:
    assert round_inward(Direction.LONG, "sl", 1.099994, 5) == 1.1
    assert round_inward(Direction.LONG, "tp", 1.100019, 5) == 1.10001
    assert round_inward(Direction.SHORT, "sl", 1.100019, 5) == 1.10001
    assert round_inward(Direction.SHORT, "tp", 1.099994, 5) == 1.1
    assert in_rollover_window(to_ns(datetime(2026, 11, 18, 22, 5, tzinfo=UTC)), 15)
    assert not in_rollover_window(to_ns(datetime(2026, 11, 18, 21, 30, tzinfo=UTC)), 15)


def test_feed_drops_forming_bar(tmp_path: Path) -> None:
    raw = bars("up")
    cal = FxCalendar()
    ds = dataset_from_bars("EURUSD", raw, 0, to_ns(SIGNAL_CLOSE) - 60 * _S, cal)
    assert ds is not None
    assert int(ds.columns["open_time_ns"][-1]) == to_ns(SIGNAL_CLOSE) - 2 * 15 * 60 * _S


# ---------------------------------------------------------------- telegram, loop, watchdog


def test_telegram_only_configured_chat_and_commands(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path, breakout=None)
    sent: list[str] = []
    updates = [
        tg.IncomingCommand(10, ""),  # another chat
        tg.IncomingCommand(11, "stato"),
        tg.IncomingCommand(12, "stop"),
        tg.IncomingCommand(13, "riattiva"),
    ]
    link = TelegramLink(
        tg.TelegramConfig("t", "42"),
        send=lambda c, text: sent.append(text),
        fetch=lambda c, off: [u for u in updates if off is None or u.update_id >= off],
    )
    run_loop(
        engine,
        store,
        telegram=link,
        clock=lambda: to_ns(NOW),
        sleep=lambda s: None,
        max_iterations=2,
        log=lambda m: None,
    )
    assert store.get_meta("telegram_offset") == "14"
    assert store.control_on(ops.KILL_SWITCH)
    assert any("FxAgent Next demo" in s for s in sent)
    assert any("Comandi:" in s for s in sent)  # help for unknown text
    assert store.unsent_alerts() == []


def test_get_commands_filters_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/stop"}},
            {"update_id": 2, "message": {"chat": {"id": 99}, "text": "/stop"}},
            {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/stato@FxBot"}},
        ],
    }

    class Resp:
        def read(self) -> bytes:
            return json.dumps(body).encode()

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp())
    got = tg.get_commands(tg.TelegramConfig("t", "42"), None)
    assert [(c.update_id, c.command) for c in got] == [(1, "stop"), (2, ""), (3, "stato")]


def test_watchdog(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite"
    assert watchdog(db, 600, to_ns(NOW)) is not None
    store = ops.OpsStore(db)
    store.beat("engine", to_ns(NOW))
    assert watchdog(db, 600, to_ns(NOW) + 60 * _S) is None
    assert "FERMO" in (watchdog(db, 600, to_ns(NOW) + 3600 * _S) or "")
    assert "Entrate" in status_text(store, to_ns(NOW))


def test_readonly_check(tmp_path: Path) -> None:
    broker, store, engine = make(tmp_path)
    out = readonly_check(broker, engine.config, to_ns(NOW))
    assert out["demo_guard"] == "OK"
    assert out["evaluations"]["EURUSD"].startswith("CANDIDATO LONG")
    assert broker.sent == []


# ---------------------------------------------------------------- MT5 adapter with a fake module


class FakeMT5:
    TIMEFRAME_M15 = 15
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_TRADE_MODE_FULL = 4

    def __init__(self) -> None:
        self.result: Any = None
        self.requests: list[dict[str, Any]] = []

    def initialize(self, *a: Any) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self) -> tuple[int, str]:
        return (-1, "x")

    def terminal_info(self) -> Any:
        return SimpleNamespace(connected=True, trade_allowed=True)

    def account_info(self) -> Any:
        return SimpleNamespace(
            login=1,
            server="NagaMarkets-Demo",
            trade_mode=0,
            currency="EUR",
            company="Naga",
            balance=1.0,
            equity=1.0,
            trade_allowed=True,
            trade_expert=True,
        )

    def symbol_info(self, s: str) -> Any:
        return SimpleNamespace(filling_mode=2)

    def order_send(self, req: dict[str, Any]) -> Any:
        self.requests.append(req)
        return self.result

    def history_deals_get(self, *a: Any, **k: Any) -> Any:
        return [SimpleNamespace(position_id=77)]


@pytest.mark.parametrize(
    ("retcode", "status"),
    [
        (10009, SendStatus.FILLED),
        (10010, SendStatus.PARTIAL),
        (10012, SendStatus.UNKNOWN),
        (10031, SendStatus.UNKNOWN),
        (10016, SendStatus.REJECTED),
        (None, SendStatus.UNKNOWN),
    ],
)
def test_mt5_retcode_mapping(retcode: int | None, status: SendStatus) -> None:
    from fxnext.ops.models import OrderRequest

    fake = FakeMT5()
    fake.result = (
        None
        if retcode is None
        else SimpleNamespace(retcode=retcode, deal=5, order=6, volume=0.01, price=1.1, comment="c")
    )
    b = MT5Broker(mt5_module=fake)
    res = b.send(
        OrderRequest("fxn-x", "EURUSD^", Direction.LONG, 0.01, 1.09, 1.12, 26111801, 1.1, 10)
    )
    assert res.status is status
    assert fake.requests[0]["type_filling"] == FakeMT5.ORDER_FILLING_IOC
    assert fake.requests[0]["comment"] == "fxn-x"
    if status is SendStatus.FILLED:
        assert res.position_ticket == 77
    acct = b.account()
    assert acct.trade_mode is AccountTradeMode.DEMO and acct.trade_allowed


# ---------------------------------------------------------------- dashboard pages and command route


def test_dashboard_demo_pages_and_command_queue(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from fxnext.dashboard.app import create_app
    from fxnext.dashboard.readers import Sources

    broker, store, engine = make(tmp_path)
    engine.step(to_ns(NOW))
    sources = Sources(research=tmp_path / "r", ops=engine.config.ops_db)
    client = TestClient(create_app(sources))
    for page in ("/oggi", "/posizioni", "/segnali"):
        resp = client.get(page)
        assert resp.status_code == 200, page
    assert "EURUSD" in client.get("/oggi").text
    bad = client.post("/oggi/comando", data={"command": "BUY", "confirm": "si"})
    assert bad.status_code == 400
    unconfirmed = client.post("/oggi/comando", data={"command": "KILL_ON"})
    assert unconfirmed.status_code == 400
    ok = client.post(
        "/oggi/comando", data={"command": "KILL_ON", "confirm": "si"}, follow_redirects=False
    )
    assert ok.status_code == 303
    engine.step(to_ns(NOW) + 5 * _S)
    assert store.control_on(ops.KILL_SWITCH)
    row = store.conn.execute("SELECT source FROM commands").fetchone()
    assert row["source"] == "dashboard"


def test_dashboard_without_engine_db(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from fxnext.dashboard.app import create_app
    from fxnext.dashboard.readers import Sources

    client = TestClient(create_app(Sources(research=tmp_path, ops=tmp_path / "none.sqlite")))
    assert "non è ancora partito" in client.get("/oggi").text
    r = client.post("/oggi/comando", data={"command": "KILL_ON", "confirm": "si"})
    assert r.status_code == 409
