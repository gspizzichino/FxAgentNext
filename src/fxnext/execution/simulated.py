"""In-memory broker with failure injection (M0 spec §8, M3 integration tests).

Semantics follow MT5: a BUY opens at the ask and its SL/TP trigger on the bid; a
SELL opens at the bid and its SL/TP trigger on the ask. The server clock is
``utc + server_offset_s``. Failures are armed by the test and consumed once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
RETCODE_REJECT = 10006
RETCODE_INVALID_STOPS = 10016
RETCODE_PARTIAL = 10010


def default_account() -> AccountIdentity:
    return AccountIdentity(
        login=1234567,
        server="NagaMarkets-Demo",
        trade_mode=AccountTradeMode.DEMO,
        currency="EUR",
        company="Simulated",
        balance=10_000.0,
        equity=10_000.0,
    )


def default_spec(symbol: str) -> SymbolSpec:
    jpy = symbol.startswith("USDJPY")
    return SymbolSpec(
        symbol=symbol,
        digits=3 if jpy else 5,
        point=0.001 if jpy else 0.00001,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=100.0,
        stops_level_points=0,
        freeze_level_points=0,
    )


@dataclass
class SimulatedBroker:
    server_offset_s: int = 0
    account_identity: AccountIdentity = field(default_factory=default_account)
    specs: dict[str, SymbolSpec] = field(default_factory=dict)
    ticks: dict[str, Tick] = field(default_factory=dict)
    bars: dict[str, list[RawBar]] = field(default_factory=dict)
    open_positions: dict[int, BrokerPosition] = field(default_factory=dict)
    history: list[BrokerDeal] = field(default_factory=list)
    # failure injection, each consumed by the next send/close
    connected: bool = True
    lose_next_response: bool = False  # executes, but the caller sees no answer
    raise_next_send: bool = False  # nothing executed, call raises
    reject_next: int | None = None
    partial_next: float | None = None  # fraction filled
    slippage_points: int = 0
    sent: list[OrderRequest] = field(default_factory=list)
    _next_ticket: int = 1000

    # ------------------------------------------------------------------ helpers for tests
    def _check(self) -> None:
        if not self.connected:
            raise BrokerUnavailable("simulated disconnect")

    def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self.specs:
            self.specs[symbol] = default_spec(symbol)
        return self.specs[symbol]

    def set_tick(self, symbol: str, utc_ns: int, bid: float, ask: float) -> None:
        """Move the market; triggers SL/TP like the MT5 server would."""

        server_ns = utc_ns + self.server_offset_s * 1_000_000_000
        self.ticks[symbol] = Tick(symbol, server_ns, bid, ask)
        for pos in list(self.open_positions.values()):
            if pos.symbol != symbol:
                continue
            if pos.direction is Direction.LONG:
                if pos.sl and bid <= pos.sl:
                    self._close(pos, pos.sl, server_ns, "SL")
                elif pos.tp and bid >= pos.tp:
                    self._close(pos, pos.tp, server_ns, "TP")
            else:
                if pos.sl and ask >= pos.sl:
                    self._close(pos, pos.sl, server_ns, "SL")
                elif pos.tp and ask <= pos.tp:
                    self._close(pos, pos.tp, server_ns, "TP")

    def add_foreign_position(self, pos: BrokerPosition) -> None:
        self.open_positions[pos.ticket] = pos

    def _ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def _pip_value(self, symbol: str, volume: float, price: float) -> float:
        # profit per unit of price move, in account currency (EUR ~ USD here)
        units = volume * 100_000
        return units / price if symbol.startswith("USDJPY") else units

    def _close(self, pos: BrokerPosition, price: float, server_ns: int, reason: str) -> None:
        sign = 1.0 if pos.direction is Direction.LONG else -1.0
        profit = sign * (price - pos.open_price) * self._pip_value(pos.symbol, pos.volume, price)
        out_dir = Direction.SHORT if pos.direction is Direction.LONG else Direction.LONG
        self.history.append(
            BrokerDeal(
                ticket=self._ticket(),
                position_id=pos.ticket,
                symbol=pos.symbol,
                entry="OUT",
                direction=out_dir,
                volume=pos.volume,
                price=price,
                server_ns=server_ns,
                magic=pos.magic,
                comment=f"[{reason.lower()} {price}]",
                profit=round(profit, 2),
                reason=reason,
            )
        )
        del self.open_positions[pos.ticket]

    # ------------------------------------------------------------------ Broker port
    def account(self) -> AccountIdentity:
        self._check()
        return self.account_identity

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._check()
        return self.spec(symbol)

    def tick(self, symbol: str) -> Tick | None:
        self._check()
        return self.ticks.get(symbol)

    def bars_m15(self, symbol: str, count: int) -> list[RawBar]:
        self._check()
        return list(self.bars.get(symbol, []))[-count:]

    def positions(self) -> list[BrokerPosition]:
        self._check()
        return list(self.open_positions.values())

    def deals(self, from_server_s: int, to_server_s: int) -> list[BrokerDeal]:
        self._check()
        lo, hi = from_server_s * 1_000_000_000, to_server_s * 1_000_000_000
        return [d for d in self.history if lo <= d.server_ns <= hi]

    def send(self, request: OrderRequest) -> SendResult:
        self._check()
        self.sent.append(request)
        if self.raise_next_send:
            self.raise_next_send = False
            raise BrokerUnavailable("simulated failure before execution")
        if self.reject_next is not None:
            code, self.reject_next = self.reject_next, None
            return SendResult(SendStatus.REJECTED, retcode=code, comment="rejected")
        tick = self.ticks.get(request.symbol)
        if tick is None:
            return SendResult(SendStatus.REJECTED, retcode=10021, comment="no prices")
        spec = self.spec(request.symbol)
        slip = self.slippage_points * spec.point
        long = request.direction is Direction.LONG
        price = tick.ask + slip if long else tick.bid - slip
        volume = request.volume
        status = SendStatus.FILLED
        retcode = RETCODE_DONE
        if self.partial_next is not None:
            volume = round(volume * self.partial_next, 2)
            self.partial_next = None
            status, retcode = SendStatus.PARTIAL, RETCODE_PARTIAL
        ticket = self._ticket()
        pos = BrokerPosition(
            ticket=ticket,
            symbol=request.symbol,
            direction=request.direction,
            volume=volume,
            open_price=price,
            open_server_ns=tick.server_ns,
            sl=request.sl,
            tp=request.tp,
            magic=request.magic,
            comment=request.client_order_id,
        )
        self.open_positions[ticket] = pos
        self.history.append(
            BrokerDeal(
                ticket=self._ticket(),
                position_id=ticket,
                symbol=request.symbol,
                entry="IN",
                direction=request.direction,
                volume=volume,
                price=price,
                server_ns=tick.server_ns,
                magic=request.magic,
                comment=request.client_order_id,
            )
        )
        if self.lose_next_response:
            self.lose_next_response = False
            raise BrokerUnavailable("simulated lost response")
        return SendResult(
            status,
            retcode=retcode,
            comment="done",
            order_ticket=ticket,
            position_ticket=ticket,
            price=price,
            volume=volume,
        )

    def close_position(
        self, position: BrokerPosition, deviation_points: int, comment: str
    ) -> SendResult:
        self._check()
        pos = self.open_positions.get(position.ticket)
        if pos is None:
            return SendResult(SendStatus.REJECTED, retcode=10036, comment="position closed")
        tick = self.ticks[pos.symbol]
        price = tick.bid if pos.direction is Direction.LONG else tick.ask
        self._close(pos, price, tick.server_ns, "EXPERT")
        return SendResult(SendStatus.FILLED, retcode=RETCODE_DONE, price=price)

    def calc_profit(
        self, symbol: str, direction: Direction, volume: float, open_price: float, close: float
    ) -> float | None:
        sign = 1.0 if direction is Direction.LONG else -1.0
        return sign * (close - open_price) * self._pip_value(symbol, volume, close)


__all__ = ["SimulatedBroker", "default_account", "default_spec"]
