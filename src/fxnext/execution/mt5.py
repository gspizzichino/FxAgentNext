"""``MT5Broker``: adapter over the ``MetaTrader5`` Python package (Windows only).

The package is imported lazily, so the rest of the project (and CI on Linux)
never needs it. It talks to the terminal already open and logged in on the PC:
no credentials pass through this code. Times are returned on the server clock.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from fxnext.domain.enums import Direction
from fxnext.execution.broker import BrokerUnavailable
from fxnext.ops.models import (
    AccountIdentity,
    AccountTradeMode,
    BrokerDeal,
    BrokerPosition,
    OrderRequest,
    RawBar,
    SendResult,
    SendStatus,
    SymbolSpec,
    Tick,
)

RETCODE_DONE = 10009
RETCODE_PLACED = 10008
RETCODE_PARTIAL = 10010
# No usable answer: the order may or may not exist -> UNKNOWN, reconcile, never resend.
RETCODES_UNKNOWN = frozenset({10012, 10031})  # TIMEOUT, CONNECTION
_DEAL_REASON = {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL", 5: "TP", 6: "SO"}
_DEAL_ENTRY = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}


def _load() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]  # noqa: N813
    except ImportError as exc:  # pragma: no cover - depends on the PC
        raise BrokerUnavailable(
            "pacchetto MetaTrader5 non installato (pip install MetaTrader5)"
        ) from exc
    return mt5


class MT5Broker:
    def __init__(self, terminal_path: str | None = None, mt5_module: Any = None) -> None:
        self.mt5 = mt5_module if mt5_module is not None else _load()
        self.terminal_path = terminal_path
        self._connected = False

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        ok = (
            self.mt5.initialize(self.terminal_path) if self.terminal_path else self.mt5.initialize()
        )
        if not ok:
            error = self.mt5.last_error()
            self._reset()
            raise BrokerUnavailable(f"initialize fallito: {error}")
        self._connected = True

    def shutdown(self) -> None:
        if self._connected:
            self.mt5.shutdown()
            self._connected = False

    def _reset(self) -> None:
        """Drop the link to the terminal, so the next call initializes it again.

        After MT5 restarts (or the PC does) the old link answers None forever:
        without this the engine would stay "disconnected" until it is restarted.
        """

        with contextlib.suppress(Exception):  # the link may already be gone
            self.mt5.shutdown()
        self._connected = False

    def _ensure(self) -> None:
        if not self._connected:
            self.connect()
        info = self.mt5.terminal_info()
        if info is None or not info.connected:
            self._reset()
            raise BrokerUnavailable("terminale non connesso al server")

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._ensure()
        out = getattr(self.mt5, name)(*args, **kwargs)
        if out is None:
            error = self.mt5.last_error()
            self._reset()
            raise BrokerUnavailable(f"{name} senza risposta: {error}")
        return out

    # ------------------------------------------------------------------ Broker port
    def account(self) -> AccountIdentity:
        a = self._call("account_info")
        term = self.mt5.terminal_info()
        return AccountIdentity(
            login=int(a.login),
            server=str(a.server),
            trade_mode=AccountTradeMode(int(a.trade_mode)),
            currency=str(a.currency),
            company=str(a.company),
            balance=float(a.balance),
            equity=float(a.equity),
            trade_allowed=bool(a.trade_allowed)
            and bool(a.trade_expert)
            and bool(term is not None and term.trade_allowed),
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._ensure()
        self.mt5.symbol_select(symbol, True)
        s = self._call("symbol_info", symbol)
        return SymbolSpec(
            symbol=symbol,
            digits=int(s.digits),
            point=float(s.point),
            volume_min=float(s.volume_min),
            volume_step=float(s.volume_step),
            volume_max=float(s.volume_max),
            stops_level_points=int(s.trade_stops_level),
            freeze_level_points=int(s.trade_freeze_level),
            trade_allowed=int(s.trade_mode) == int(self.mt5.SYMBOL_TRADE_MODE_FULL),
        )

    def tick(self, symbol: str) -> Tick | None:
        self._ensure()
        t = self.mt5.symbol_info_tick(symbol)
        if t is None or not t.time_msc:
            return None
        return Tick(symbol, int(t.time_msc) * 1_000_000, float(t.bid), float(t.ask))

    def bars_m15(self, symbol: str, count: int) -> list[RawBar]:
        self._ensure()
        self.mt5.symbol_select(symbol, True)
        rates = self._call("copy_rates_from_pos", symbol, self.mt5.TIMEFRAME_M15, 0, count)
        return [
            RawBar(
                int(r["time"]),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["tick_volume"]),
            )
            for r in rates
        ]

    def positions(self) -> list[BrokerPosition]:
        rows = self._call("positions_get")
        return [
            BrokerPosition(
                ticket=int(p.ticket),
                symbol=str(p.symbol),
                direction=Direction.LONG if int(p.type) == 0 else Direction.SHORT,
                volume=float(p.volume),
                open_price=float(p.price_open),
                open_server_ns=int(p.time_msc) * 1_000_000,
                sl=float(p.sl),
                tp=float(p.tp),
                magic=int(p.magic),
                comment=str(p.comment),
                swap=float(p.swap),
                profit=float(p.profit),
            )
            for p in rows
        ]

    def deals(self, from_server_s: int, to_server_s: int) -> list[BrokerDeal]:
        rows = self._call("history_deals_get", from_server_s, to_server_s)
        out = []
        for d in rows:
            if int(d.type) not in (0, 1):  # balance, credit... are not trades
                continue
            out.append(
                BrokerDeal(
                    ticket=int(d.ticket),
                    position_id=int(d.position_id),
                    symbol=str(d.symbol),
                    entry=_DEAL_ENTRY.get(int(d.entry), str(d.entry)),
                    direction=Direction.LONG if int(d.type) == 0 else Direction.SHORT,
                    volume=float(d.volume),
                    price=float(d.price),
                    server_ns=int(d.time_msc) * 1_000_000,
                    magic=int(d.magic),
                    comment=str(d.comment),
                    profit=float(d.profit),
                    swap=float(d.swap),
                    commission=float(d.commission) + float(getattr(d, "fee", 0.0)),
                    reason=_DEAL_REASON.get(int(d.reason), str(d.reason)),
                )
            )
        return out

    def _filling(self, symbol: str) -> int:
        info = self.mt5.symbol_info(symbol)
        mode = int(getattr(info, "filling_mode", 0)) if info is not None else 0
        if mode & 1:
            return int(self.mt5.ORDER_FILLING_FOK)
        if mode & 2:
            return int(self.mt5.ORDER_FILLING_IOC)
        return int(self.mt5.ORDER_FILLING_RETURN)

    def _send(self, request: dict[str, Any]) -> SendResult:
        sent = time.time_ns()
        try:
            res = self.mt5.order_send(request)
        except Exception as exc:  # noqa: BLE001 - the package can raise on IPC errors
            return SendResult(SendStatus.UNKNOWN, comment=f"eccezione: {exc}", sent_at_ns=sent)
        answered = time.time_ns()
        if res is None:
            return SendResult(
                SendStatus.UNKNOWN,
                comment=f"nessuna risposta: {self.mt5.last_error()}",
                sent_at_ns=sent,
                answered_at_ns=answered,
            )
        code = int(res.retcode)
        if code in (RETCODE_DONE, RETCODE_PLACED):
            status = SendStatus.FILLED
        elif code == RETCODE_PARTIAL:
            status = SendStatus.PARTIAL
        elif code in RETCODES_UNKNOWN:
            status = SendStatus.UNKNOWN
        else:
            status = SendStatus.REJECTED
        position = None
        if status in (SendStatus.FILLED, SendStatus.PARTIAL) and int(res.deal):
            deals = self.mt5.history_deals_get(ticket=int(res.deal))
            if deals:
                position = int(deals[0].position_id)
        return SendResult(
            status,
            retcode=code,
            comment=str(res.comment),
            order_ticket=int(res.order) or None,
            position_ticket=position or (int(res.order) or None),
            price=float(res.price) or None,
            volume=float(res.volume) or None,
            sent_at_ns=sent,
            answered_at_ns=answered,
        )

    def send(self, request: OrderRequest) -> SendResult:
        self._ensure()
        buy = request.direction is Direction.LONG
        return self._send(
            {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": request.symbol,
                "volume": float(request.volume),
                "type": self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL,
                "price": float(request.reference_price),
                "sl": float(request.sl),
                "tp": float(request.tp),
                "deviation": int(request.deviation_points),
                "magic": int(request.magic),
                "comment": request.client_order_id,
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(request.symbol),
            }
        )

    def close_position(
        self, position: BrokerPosition, deviation_points: int, comment: str
    ) -> SendResult:
        self._ensure()
        t = self._call("symbol_info_tick", position.symbol)
        buy = position.direction is Direction.LONG
        return self._send(
            {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "position": int(position.ticket),
                "volume": float(position.volume),
                "type": self.mt5.ORDER_TYPE_SELL if buy else self.mt5.ORDER_TYPE_BUY,
                "price": float(t.bid if buy else t.ask),
                "deviation": int(deviation_points),
                "magic": int(position.magic),
                "comment": comment[:31],
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(position.symbol),
            }
        )

    def calc_profit(
        self, symbol: str, direction: Direction, volume: float, open_price: float, close: float
    ) -> float | None:
        self._ensure()
        kind = self.mt5.ORDER_TYPE_BUY if direction is Direction.LONG else self.mt5.ORDER_TYPE_SELL
        out = self.mt5.order_calc_profit(kind, symbol, float(volume), open_price, close)
        return None if out is None else float(out)


__all__ = ["MT5Broker", "RETCODES_UNKNOWN"]
