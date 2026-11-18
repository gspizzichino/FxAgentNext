"""Demo guard, server clock and preflight (M0 spec §7).

``demo_guard`` runs before every order and every close. It accepts only an MT5
account whose terminal reports ``ACCOUNT_TRADE_MODE_DEMO`` *and* whose server
and login are in the allowlist. Nothing in the configuration can relax it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from fxnext.domain.calendar import FxCalendar, ny_1700_utc
from fxnext.domain.enums import Direction
from fxnext.domain.timeutil import from_ns
from fxnext.execution.config import LEGACY_MAGIC, DemoConfig
from fxnext.ops.models import AccountIdentity, AccountTradeMode, SymbolSpec, Tick

_NS = 1_000_000_000
_QUARTER_S = 15 * 60
PIP_SIZE = {"EURUSD": 1e-4, "GBPUSD": 1e-4, "USDJPY": 1e-2}


class NotDemoAccount(RuntimeError):
    pass


def demo_guard(account: AccountIdentity, config: DemoConfig) -> None:
    """Raise ``NotDemoAccount`` unless this is an allowlisted demo account."""

    problems = []
    if account.trade_mode is not AccountTradeMode.DEMO:
        problems.append(f"trade_mode={account.trade_mode.name}")
    if account.server not in config.allowed_servers:
        problems.append(f"server {account.server!r} not allowed")
    if account.login not in config.allowed_logins:
        problems.append(f"login {account.login} not allowed")
    if account.currency != config.account_currency:
        problems.append(f"currency {account.currency}")
    if config.magic == LEGACY_MAGIC:
        problems.append("legacy magic")
    if problems:
        raise NotDemoAccount("; ".join(problems))


@dataclass
class ServerClock:
    """Server offset detected from fresh ticks, rounded to the quarter hour (ADR-0002)."""

    offset_s: int | None = None

    @staticmethod
    def estimate(tick: Tick, utc_now_ns: int) -> int:
        raw = tick.server_ns / _NS - utc_now_ns / _NS
        return int(round(raw / _QUARTER_S)) * _QUARTER_S

    def to_utc_ns(self, server_ns: int) -> int:
        if self.offset_s is None:
            raise RuntimeError("server offset not detected yet")
        return server_ns - self.offset_s * _NS

    def to_server_s(self, utc_ns: int) -> int:
        if self.offset_s is None:
            raise RuntimeError("server offset not detected yet")
        return utc_ns // _NS + self.offset_s


def round_inward(direction: Direction, kind: str, level: float, digits: int) -> float:
    """Round a stop or target to broker digits, only towards the entry (less risk/target)."""

    scale = 10.0**digits
    toward_up = (direction is Direction.LONG) == (kind == "sl")
    value = math.ceil(level * scale - 1e-6) if toward_up else math.floor(level * scale + 1e-6)
    return round(value / scale, digits)


def in_rollover_window(now_ns: int, minutes: int) -> bool:
    """True within ``minutes`` of the daily 17:00 New York rollover."""

    now = from_ns(now_ns)
    for d in (now.date() - timedelta(days=1), now.date(), now.date() + timedelta(days=1)):
        if abs(now - ny_1700_utc(d)) <= timedelta(minutes=minutes):
            return True
    return False


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reason: str = ""
    sl: float = 0.0
    tp: float = 0.0
    reference_price: float = 0.0
    spread_pips: float = 0.0


def preflight(
    *,
    symbol: str,
    direction: Direction,
    stop: float,
    target: float,
    volume: float,
    expires_ns: int,
    now_ns: int,
    tick: Tick | None,
    tick_utc_ns: int | None,
    spec: SymbolSpec,
    account: AccountIdentity,
    config: DemoConfig,
    calendar: FxCalendar,
) -> PreflightResult:
    def no(reason: str) -> PreflightResult:
        return PreflightResult(False, reason)

    if not account.trade_allowed or not spec.trade_allowed:
        return no("trading not allowed by terminal")
    if now_ns >= expires_ns:
        return no("candidate expired")
    if not calendar.is_in_session(from_ns(now_ns)):
        return no("market closed")
    if in_rollover_window(now_ns, config.rollover_quiet_minutes):
        return no("rollover window")
    if volume > config.max_volume or volume < spec.volume_min or volume > spec.volume_max:
        return no(f"volume {volume} outside limits")
    steps = volume / spec.volume_step
    if abs(steps - round(steps)) > 1e-6:
        return no(f"volume {volume} not a multiple of {spec.volume_step}")
    if tick is None or tick_utc_ns is None:
        return no("no tick")
    age_s = (now_ns - tick_utc_ns) / _NS
    if age_s > config.max_tick_age_s or age_s < -config.max_tick_age_s:
        return no(f"stale tick ({age_s:.0f}s)")
    if tick.ask < tick.bid or tick.bid <= 0:
        return no("invalid tick")
    pip = PIP_SIZE[symbol]
    spread_pips = (tick.ask - tick.bid) / pip
    if spread_pips > config.max_spread_pips[symbol]:
        return no(f"spread {spread_pips:.1f} pips > {config.max_spread_pips[symbol]}")
    sl = round_inward(direction, "sl", stop, spec.digits)
    tp = round_inward(direction, "tp", target, spec.digits)
    min_dist = max(spec.stops_level_points, spec.freeze_level_points) * spec.point
    # MT5: BUY stops are checked against the bid, SELL stops against the ask
    if direction is Direction.LONG:
        ref, trigger = tick.ask, tick.bid
        if not (sl < trigger - min_dist and tp > trigger + min_dist):
            return no("stop/target too close or already crossed")
    else:
        ref, trigger = tick.bid, tick.ask
        if not (sl > trigger + min_dist and tp < trigger - min_dist):
            return no("stop/target too close or already crossed")
    return PreflightResult(True, "", sl, tp, ref, spread_pips)


__all__ = [
    "NotDemoAccount",
    "PIP_SIZE",
    "PreflightResult",
    "ServerClock",
    "demo_guard",
    "in_rollover_window",
    "preflight",
    "round_inward",
]
