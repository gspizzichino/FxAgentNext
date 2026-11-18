"""The ``Broker`` port (M0 spec §5, M3). Implemented by ``MT5Broker`` and ``SimulatedBroker``.

All instants returned by a broker are on the broker *server* clock; the engine
converts them with the detected server offset (ADR-0002). A broker never decides
anything: it reports state and executes exactly one request per ``send``/``close``.
"""

from __future__ import annotations

from typing import Protocol

from fxnext.domain.enums import Direction
from fxnext.ops.models import (
    AccountIdentity,
    BrokerDeal,
    BrokerPosition,
    OrderRequest,
    RawBar,
    SendResult,
    SymbolSpec,
    Tick,
)


class BrokerUnavailable(RuntimeError):
    """Terminal not connected, not logged in, or the call failed without an answer."""


class Broker(Protocol):
    def account(self) -> AccountIdentity: ...

    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    def tick(self, symbol: str) -> Tick | None: ...

    def bars_m15(self, symbol: str, count: int) -> list[RawBar]:
        """The last ``count`` *closed and open* M15 candles, oldest first."""
        ...

    def positions(self) -> list[BrokerPosition]: ...

    def deals(self, from_server_s: int, to_server_s: int) -> list[BrokerDeal]: ...

    def send(self, request: OrderRequest) -> SendResult: ...

    def close_position(
        self, position: BrokerPosition, deviation_points: int, comment: str
    ) -> SendResult: ...

    def calc_profit(
        self, symbol: str, direction: Direction, volume: float, open_price: float, close: float
    ) -> float | None:
        """Profit in account currency of a hypothetical trade (used for the R unit)."""
        ...


__all__ = ["Broker", "BrokerUnavailable"]
