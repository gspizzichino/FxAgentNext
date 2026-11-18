"""M3 contracts (contracts §14): account, symbol, broker state, intents.

Plain frozen dataclasses: they cross the broker boundary and the SQLite store, they
are never hashed into research outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from fxnext.domain.enums import Direction


class AccountTradeMode(IntEnum):
    """Values of MT5 ``ACCOUNT_TRADE_MODE_*``."""

    DEMO = 0
    CONTEST = 1
    REAL = 2


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    login: int
    server: str
    trade_mode: AccountTradeMode
    currency: str
    company: str = ""
    balance: float = 0.0
    equity: float = 0.0
    trade_allowed: bool = True


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    symbol: str  # broker symbol (e.g. "EURUSD^")
    digits: int
    point: float
    volume_min: float
    volume_step: float
    volume_max: float
    stops_level_points: int
    freeze_level_points: int
    trade_allowed: bool = True


@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str  # broker symbol
    server_ns: int  # broker server clock (MT5 time_msc), converted by the engine
    bid: float
    ask: float


@dataclass(frozen=True, slots=True)
class RawBar:
    """One broker candle; ``open_server_s`` is on the broker server clock."""

    open_server_s: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: float


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    ticket: int
    symbol: str  # broker symbol
    direction: Direction
    volume: float
    open_price: float
    open_server_ns: int
    sl: float
    tp: float
    magic: int
    comment: str
    swap: float = 0.0
    profit: float = 0.0


@dataclass(frozen=True, slots=True)
class BrokerDeal:
    ticket: int
    position_id: int
    symbol: str
    entry: str  # "IN" | "OUT"
    direction: Direction  # direction of the deal itself
    volume: float
    price: float
    server_ns: int
    magic: int
    comment: str
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    reason: str = ""  # "SL" | "TP" | "CLIENT" | "EXPERT" | ...


class SendStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # no usable answer: never resend, reconcile


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str  # <= 31 chars, goes in the MT5 comment
    symbol: str  # broker symbol
    direction: Direction
    volume: float
    sl: float
    tp: float
    magic: int
    reference_price: float  # price used for the deviation check
    deviation_points: int


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    retcode: int | None = None
    comment: str = ""
    order_ticket: int | None = None
    position_ticket: int | None = None
    price: float | None = None
    volume: float | None = None
    sent_at_ns: int | None = None
    answered_at_ns: int | None = None


class IntentState(StrEnum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILED_FILLED = "RECONCILED_FILLED"
    RECONCILED_ABSENT = "RECONCILED_ABSENT"
    BLOCKED = "BLOCKED"  # stopped by preflight/risk before any send


OPEN_INTENT_STATES = frozenset({IntentState.PENDING, IntentState.SUBMITTING, IntentState.UNKNOWN})
FILLED_STATES = frozenset({IntentState.FILLED, IntentState.PARTIAL, IntentState.RECONCILED_FILLED})


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    volume: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    ours: tuple[BrokerPosition, ...] = ()
    foreign: tuple[BrokerPosition, ...] = ()
    adopted: tuple[int, ...] = ()
    closed: tuple[int, ...] = ()
    unknown_resolved: tuple[str, ...] = ()
    discrepancies: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return not self.discrepancies
