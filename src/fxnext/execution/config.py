"""Demo runtime configuration (TOML on the PC, never committed with real logins).

There is deliberately no ``mode`` field: this package only knows demo accounts.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Magic number of the old FxAgent on the same terminal: never reuse it.
LEGACY_MAGIC = 260828


@dataclass(frozen=True)
class DemoConfig:
    allowed_logins: tuple[int, ...]
    allowed_servers: tuple[str, ...] = ("NagaMarkets-Demo",)
    account_currency: str = "EUR"
    symbols: dict[str, str] = field(
        default_factory=lambda: {"EURUSD": "EURUSD^", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY"}
    )
    volume: float = 0.01
    max_volume: float = 0.01
    max_positions_total: int = 3
    daily_limit_r: float = -5.0
    weekly_limit_r: float = -10.0
    magic: int = 26111801
    deviation_points: int = 10
    max_spread_pips: dict[str, float] = field(
        default_factory=lambda: {"EURUSD": 2.0, "GBPUSD": 3.0, "USDJPY": 2.5}
    )
    max_tick_age_s: float = 60.0
    evaluation_delay_s: float = 10.0
    max_lateness_s: float = 300.0
    rollover_quiet_minutes: int = 15
    time_exit_slots: int = 120
    reconcile_tolerance_s: float = 120.0
    bars_lookback: int = 600
    loop_seconds: float = 5.0
    ops_db: Path = Path("data/ops/demo.sqlite")

    def __post_init__(self) -> None:
        if not self.allowed_logins:
            raise ValueError("allowed_logins must list the demo login(s)")
        if self.magic == LEGACY_MAGIC:
            raise ValueError("magic 260828 belongs to the old FxAgent")
        if not 0 < self.volume <= self.max_volume <= 0.1:
            raise ValueError("volume must be > 0 and <= max_volume <= 0.1 lots")
        if self.daily_limit_r >= 0 or self.weekly_limit_r >= 0:
            raise ValueError("loss limits must be negative R values")
        if self.max_positions_total < 1:
            raise ValueError("max_positions_total must be >= 1")
        missing = set(self.symbols) - set(self.max_spread_pips)
        if missing:
            raise ValueError(f"max_spread_pips missing for {sorted(missing)}")

    @classmethod
    def load(cls, path: Path) -> DemoConfig:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
        account = data.get("account", {})
        risk = data.get("risk", {})
        execution = data.get("execution", {})
        kwargs: dict[str, Any] = {
            "allowed_logins": tuple(int(x) for x in account.get("logins", ())),
        }
        if "servers" in account:
            kwargs["allowed_servers"] = tuple(str(x) for x in account["servers"])
        if "currency" in account:
            kwargs["account_currency"] = str(account["currency"])
        if "symbols" in data:
            kwargs["symbols"] = {str(k): str(v) for k, v in data["symbols"].items()}
        for key in ("volume", "max_volume", "daily_limit_r", "weekly_limit_r"):
            if key in risk:
                kwargs[key] = float(risk[key])
        if "max_positions_total" in risk:
            kwargs["max_positions_total"] = int(risk["max_positions_total"])
        ints = ("magic", "deviation_points", "rollover_quiet_minutes", "time_exit_slots")
        for key in ints:
            if key in execution:
                kwargs[key] = int(execution[key])
        floats = (
            "max_tick_age_s",
            "evaluation_delay_s",
            "max_lateness_s",
            "reconcile_tolerance_s",
            "loop_seconds",
        )
        for key in floats:
            if key in execution:
                kwargs[key] = float(execution[key])
        if "max_spread_pips" in execution:
            kwargs["max_spread_pips"] = {
                str(k): float(v) for k, v in execution["max_spread_pips"].items()
            }
        if "ops_db" in execution:
            kwargs["ops_db"] = Path(execution["ops_db"])
        return cls(**kwargs)


EXAMPLE_TOML = """\
# fxnext demo: configuration of the engine on the Naga DEMO account.
# Only demo accounts are accepted; there is no setting for a real account.
[account]
logins = [0]                     # your demo login, from MT5 (File > Login)
servers = ["NagaMarkets-Demo"]
currency = "EUR"

[symbols]                        # research symbol -> broker symbol
EURUSD = "EURUSD^"
GBPUSD = "GBPUSD"
USDJPY = "USDJPY"

[risk]
volume = 0.01
max_volume = 0.01
max_positions_total = 3
daily_limit_r = -5.0
weekly_limit_r = -10.0

[execution]
magic = 26111801                 # must differ from the old FxAgent (260828)
deviation_points = 10
max_tick_age_s = 60
evaluation_delay_s = 10
max_lateness_s = 300
rollover_quiet_minutes = 15
time_exit_slots = 120
ops_db = "data/ops/demo.sqlite"

[execution.max_spread_pips]
EURUSD = 2.0
GBPUSD = 3.0
USDJPY = 2.5
"""

__all__ = ["DemoConfig", "EXAMPLE_TOML", "LEGACY_MAGIC"]
